#!/usr/bin/env python
# coding: utf-8

DATABASE = 'CCA'  # 'BULB' or 'CCA'
PREDICT_ONLY_IM = False
BATCH_SIZE = 4
INPUT_SHAPE = (512, 512)

BACKBONE = 'efficientnetb0'
LR = 0.0001
LOAD_PRETRAINED_MODEL = True

TRAINING_PARAMETERS = {
    'steps_per_epoch': 200,
    'epochs': 40,
    'validation_steps': 35,
    'use_multiprocessing': False,
    'max_queue_size': 10,
    'workers': 8,
    'verbose': 1}