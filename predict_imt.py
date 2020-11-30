#!/usr/bin/env python
# coding: utf-8
import os

import cv2
import numpy as np
import pandas as pd
import tensorflow as tf
from matplotlib import pyplot as plt
from tensorflow.keras import backend as keras_backend
from tensorflow.keras.callbacks import ModelCheckpoint, TensorBoard, ReduceLROnPlateau, EarlyStopping
from tensorflow.keras.metrics import Recall
from tensorflow.keras.mixed_precision import experimental as mixed_precision
from tensorflow.keras.optimizers import Adam

import config
import helpers
from data_generators import data_generator
from helpers import add_previous_results, filter_dataframe
from models import get_imt_prediction_model


def weighted_bce(y_true, y_pred):
    """
    Weighted binary cross-entropy loss for training of unbalanced plaque class classification
    :param y_true: tensor of gt
    :param y_pred: tensor of predicted values
    :return: weighted binary cross-entropy between gt and predictions
    """
    weights = (y_true * 10.) + 1.
    bce = keras_backend.binary_crossentropy(y_true, y_pred)
    weighted_bce = keras_backend.mean(bce * weights)
    return weighted_bce


def nn_predict_imt(img_path, mask_path, model, input_shape, target_columns):
    """
    Predicts the IMT for an image given its path
    :param img_path: path to the image to predict. If the original image is not an input, replace with None
    :param mask_path: path to the mask to predict. If the mask is not an input, replace with None
    :param model: tensorflow model for IMT prediction
    :param target_columns: name of the columns forming the output
    :param input_shape: shape of the input image
    :return: predicted IMT values, specific targets depends on the model
    """
    if img_path is not None:
        img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE) / 255.
        img = cv2.resize(img, input_shape)
        input_data = img
    if mask_path is not None:
        mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE) / 255.
        mask = cv2.resize(mask, input_shape)
        input_data = mask
    if img_path is not None and mask_path is not None:
        input_data = np.dstack((img, mask))
    prediction = model.predict(np.expand_dims(input_data, axis=0))

    result = {}
    count = 0
    for key, value in target_columns.items():
        if value['predict']:
            result[key] = np.squeeze(prediction[count])
            count += 1
    return result


def plot_predictions(model, generator, plot_images=False, loops=1):
    """
    Predicts batches from a specified generator to analyze the output.
    :param model: tensorflow model
    :param generator: generator to evaluate
    :param plot_images: boolean indicating if results should be plotted. If batch size is high, it could be impractical
    :param loops: number of batches to predict on
    """
    errors = []
    for x_batch, y_batch in generator:
        for i in range(generator.batch_size):
            if config.PREDICT_PLAQUE:  # TODO:Update
                gt = [y_batch[0][i], y_batch[1][i], y_batch[2][i]]
            else:
                gt = [y_batch[0][i], y_batch[1][i]]
            pred = model.predict(np.expand_dims(x_batch[i], axis=0))
            pred = np.squeeze(np.array(pred).tolist())
            error = gt - pred
            errors.append(error)
            if plot_images:
                plt.imshow(np.squeeze(x_batch[i]))
                plt.show()
            print('GT:        {}'.format(gt))
            print('Predicted: {}'.format(pred))
            print('error:     {}'.format(error))

        loops -= 1
        if not loops:
            break
    print('Mean error: {}'.format(sum(errors) / len(errors)))


def predict_complete_dataframe(model, dataframe, input_column, target_columns, input_shape, debug=False):
    """
    Evaluates model on train, validation and test data.
    :param target_columns: name of the columns forming the output
    :param debug: boolean indicating if extra information should be printed for debugging purposes
    :param input_shape: shape of the input image
    :param model: tensorflow model
    :param dataframe: dataframe containing information relevant to the experiment
    :param input_column: name of the column containing the paths to the input images
    :return:
    """

    # Can be optimized using a generator, but I found ordering errors in tf 2.2 complete generator with shuffle=False
    print('Predicting values from the complete dataframe, this could take a while')
    nn_predictions = []
    for index, row in dataframe.iterrows():
        img_path = None if input_column == 'mask_path' else row['complete_path']
        mask_path = None if input_column == 'complete_path' else row['mask_path']
        nn_predictions.append(
            nn_predict_imt(img_path=img_path, mask_path=mask_path, model=model, input_shape=input_shape,
                           target_columns=target_columns))
    dataframe['nn_prediction'] = nn_predictions
    for key, value in target_columns.items():
        if value['predict']:
            prediction_column_name = 'predicted_{}'.format(key)
            dataframe[prediction_column_name] = dataframe['nn_prediction'].apply(lambda x: x[key])
    return dataframe


def train_imt_predictor(database=config.DATABASE, input_type=config.INPUT_TYPE, input_shape=config.INPUT_SHAPE,
                        target_columns=config.TARGET_COLUMNS, compare_results=config.COMPARE_RESULTS,
                        random_seed=config.RANDOM_SEED, learning_rate=config.LEARNING_RATE, debug=config.DEBUG,
                        train=config.TRAIN, use_mixed_precision=config.MIXED_PRECISION, epochs=config.EPOCHS,
                        batch_size=config.BATCH_SIZE, early_stopping_patience=config.EARLY_STOPPING_PATIENCE,
                        n_workers=config.WORKERS, max_queue_size=config.MAX_QUEUE_SIZE,
                        data_augmentation_params=config.DATA_AUGMENTATION_PARAMS, train_percent=config.TRAIN_PERCENTAGE,
                        valid_percent=config.VAL_PERCENTAGE, test_percent=config.TEST_PERCENTAGE,
                        resume_training=config.RESUME_TRAINING, silent_mode=config.SILENT_MODE,
                        prefix=config.EXPERIMENT_PREFIX):
    """
    Complete training pipeline. Values can be set on the config.py or directly on function call.

    :param database: string representing the database. Could be 'CCA' or 'BULB'
    :param input_type: string indicating if the input of the network should be the original image or the segmented mask
    :param input_shape: tuple containing the shape of the input image
    :param target_columns: name of the columns forming the output
    :param compare_results: boolean indicating if results should be compared to the ones in M.d.M Vila et al.
    :param random_seed: number used to ensure reproducibility of results
    :param learning_rate: starting learning rate value for the model
    :param debug: boolean indicating if extra information should be printed for debugging purposes
    :param train: boolean indicating if the network should be trained. If False, only the evaluation will be performed
    :param use_mixed_precision: boolean indicating if mixed precision is used. Compute capability >7.0 is required.
    :param epochs: number of passes through the complete data-set in the training process.
    :param batch_size: size of batches generated by the generator
    :param early_stopping_patience: max number of epochs without improvements in val_loss
    :param n_workers: number of CPU cores used in the training process. A value of -1 will use all available ones
    :param max_queue_size: maximum number of batches to pre-compute to avoid bottlenecks.
    :param data_augmentation_params: dict containing data augmentation for training. See tf ImageDataGenerator
    :param train_percent: percentage of values used for training
    :param valid_percent: percentage of values used for validation
    :param test_percent: percentage of values used for testing
    :param resume_training: boolean indicating if previous best performing model should be loaded before training
    :param silent_mode: boolean indicating if all outputs should be suppressed
    :param prefix: string to distinguish between experiments

    """

    # Define experiment id
    output_id = '_'.join([key.replace('imt_', '') for key, value in target_columns.items() if value['predict']])
    experiment_id = '{}_{}_{}_{}_{}'.format(prefix, database, input_type, input_shape[0], output_id)
    print("Experiment id: {}".format(experiment_id))

    # Set random seeds
    tf.random.set_seed(random_seed)
    np.random.seed(random_seed)

    # Load data from disk
    data = np.load(os.path.join('segmentation', 'complete_data_{}.npy'.format(database)), allow_pickle=True)
    data = data[()]['data']

    # Convert to dataframe and filter invalid values
    df = pd.DataFrame.from_dict(data, orient='index')
    df = filter_dataframe(df, database)

    # Change index format #TODO: fix in previous step
    df.index = df.index.map(lambda x: x[4:-1])
    if database == 'CCA':
        df['mask_path'] = df['mask_path'].apply(lambda x: "segmentation/{}".format(x))  # TODO: Fix

    if compare_results:
        # Add columns with results from https://doi.org/10.1016/j.artmed.2019.101784
        df = add_previous_results(df, database=database)

    df['gt_plaque'] = df['gt_imt_max'].apply(lambda x: 1 if x >= 1.5 else 0)

    device_name = tf.test.gpu_device_name()
    if config.FORCE_GPU:
        if device_name != '/device:GPU:0':
            raise SystemError('GPU device not found')
        if not silent_mode:
            print('Found GPU at: {}'.format(device_name))

    if input_type == 'img':
        input_column = 'complete_path'
    elif input_type == 'mask':
        input_column = 'mask_path'
    elif input_type == 'img_and_mask':
        input_column = input_type
    else:
        raise NotImplementedError

    # Shuffle dataframe
    df = df.sample(frac=1, random_state=random_seed).reset_index(drop=True)

    selected_columns = ['gt_' + key for key, value in target_columns.items() if value['predict']]
    n_outputs = len(selected_columns)
    if n_outputs == 1:
        target_column = selected_columns[0]
    else:
        df['target_column'] = df[selected_columns].values.tolist()
        df['target_column'] = df['target_column'].to_numpy()
        target_column = 'target_column'

    df_train, df_valid, df_test, df = helpers.train_validate_test_split(df, train_percent=train_percent,
                                                                        validate_percent=valid_percent,
                                                                        test_percent=test_percent)
    model = get_imt_prediction_model()

    weights_path = 'checkpoints/weights_{}.h5'.format(experiment_id)
    if resume_training:
        if os.path.exists(weights_path):
            model.load_weights(weights_path)

    optimizer = Adam(lr=learning_rate)

    # Define losses and weights depending on number of outputs
    losses = []
    metrics = {}
    loss_weights = []

    for key, value in target_columns.items():
        if value['predict']:
            if key == 'plaque':
                metrics['plaque'] = [Recall(name='recall'), 'accuracy']
            losses.append(value['loss'])
            loss_weights.append(value['weight'])

    model.compile(optimizer=optimizer, loss=losses, loss_weights=loss_weights, metrics=metrics)


    # Define data generators
    train_generator = data_generator(mode='train', dataframe=df_train, input_column=input_column,
                                     target_column=target_column, batch_size=batch_size,
                                     data_augmentation_params=data_augmentation_params, input_shape=input_shape
                                     , n_outputs=n_outputs, seed=config.RANDOM_SEED)
    valid_generator = data_generator(mode='valid', dataframe=df_valid, input_column=input_column,
                                     target_column=target_column, batch_size=batch_size, input_shape=input_shape
                                     , n_outputs=n_outputs, seed=config.RANDOM_SEED)
    test_generator = data_generator(mode='test', dataframe=df_test, input_column=input_column,
                                    target_column=target_column, batch_size=batch_size, input_shape=input_shape
                                    , n_outputs=n_outputs, seed=config.RANDOM_SEED)

    if debug:
        helpers.test_generator_output(test_generator, n_images=2)

    # Define callbacks
    os.makedirs('checkpoints', exist_ok=True)

    if train:
        callbacks = [ModelCheckpoint(filepath=weights_path, save_best_only=True, verbose=True, monitor='val_loss'),
                     TensorBoard(
                         log_dir='logs/run_{}.h5'.format(experiment_id), profile_batch=0, write_graph=False),
                     ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=15, min_lr=1e-6, verbose=True),
                     EarlyStopping(monitor='val_loss', patience=early_stopping_patience)]

        # Mixed precision can speedup the training process and lower the memory usage, CC>7 required
        if use_mixed_precision:
            policy = mixed_precision.Policy('mixed_float16')
            mixed_precision.set_policy(policy)

        # Define number of steps for the generator to cover all the data
        TRAINING_STEPS = train_generator.n // train_generator.batch_size
        VALIDATION_STEPS = valid_generator.n // valid_generator.batch_size

        # Execute training
        history = model.fit(train_generator,
                            steps_per_epoch=TRAINING_STEPS,
                            validation_data=valid_generator,
                            validation_steps=VALIDATION_STEPS,
                            epochs=epochs,
                            max_queue_size=max_queue_size,
                            workers=n_workers,
                            use_multiprocessing=False,  # tf 2.2 recommends using tf.data for multiprocessing
                            callbacks=callbacks)
        if not silent_mode:
            helpers.plot_training_history(history, experiment_id)

    # Load the best performing weights for the validation set
    model.load_weights(weights_path)

    model_path = 'models/model_{}.h5'.format(experiment_id)

    if debug:
        plot_predictions(model, test_generator)
    if not silent_mode:
        mode_list = [key for key, value in target_columns.items() if value['predict']]
        df = predict_complete_dataframe(model=model, dataframe=df, input_column=input_column, input_shape=input_shape,
                                        debug=debug, target_columns=target_columns)
        helpers.evaluate_performance(dataframe=df, mode_list=mode_list, compare_results=compare_results,
                                     exp_id=experiment_id)

    helpers.save_model(model, model_path)


if __name__ == '__main__':
    train_imt_predictor()
