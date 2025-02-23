"""
Derived from keras-yolo3 train.py (https://github.com/qqwweee/keras-yolo3),
with additions from https://github.com/AntonMu/TrainYourOwnYOLO.
"""

import os
import sys
import argparse
import pickle

import numpy as np
import keras.backend as K

from keras.layers import Input, Lambda
from keras.models import Model
from keras.optimizers import Adam
from keras.callbacks import TensorBoard, ModelCheckpoint, ReduceLROnPlateau, EarlyStopping

from PIL import Image
from time import time

from yolo3.model import preprocess_true_boxes, yolo_body, tiny_yolo_body, yolo_loss
from yolo3.utils import get_random_data


def get_curr_dir():
    return os.path.dirname(os.path.abspath(__file__))

def get_parent_dir(n=1):
    """ 
    returns the n-th parent dicrectory of the current
    working directory 
    """
    current_path = get_curr_dir()
    for k in range(n):
        current_path = os.path.dirname(current_path)
    return current_path

# --- global constants

EXPORT_DIR = os.path.join(get_parent_dir(), 'for_yolo', 'vott', 'vott-export')
ANNOT_FILE = os.path.join(EXPORT_DIR, 'yolo_annotations.txt')

WEIGHTS_DIR = os.path.join(get_curr_dir(), 'model_data')
YOLO_CLASSES = os.path.join(EXPORT_DIR, 'classes.names')

LOG_DIR = 'logs/000/'
ANCHORS_PATH = os.path.join(WEIGHTS_DIR, 'yolo_anchors.txt')
WEIGHTS_PATH = os.path.join(WEIGHTS_DIR, 'yolo_weights.h5')

VAL_SPLIT = 0.1   # 10% validation data
EPOCHS = 102      # number of epochs to train; 50% transfer, 50% fine-tuning


def _main():
    class_names = get_classes(YOLO_CLASSES)
    num_classes = len(class_names)

    anchors = get_anchors(ANCHORS_PATH)

    input_shape = (416, 416)                    # multiple of 32, height, width
    epoch1, epoch2 = EPOCHS // 2, EPOCHS // 2

    model = create_model(input_shape, anchors, num_classes,
        freeze_body=2, weights_path=WEIGHTS_PATH) # make sure you know what you freeze

    logging = TensorBoard(log_dir=LOG_DIR)
    checkpoint = ModelCheckpoint(LOG_DIR + 'ep{epoch:03d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5',
        monitor='val_loss', save_weights_only=True, save_best_only=True, period=3)
    reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=3, verbose=1)
    early_stopping = EarlyStopping(monitor='val_loss', min_delta=0, patience=10, verbose=1)

    with open(ANNOT_FILE) as f:
        lines = f.readlines()

    np.random.seed(10101)
    np.random.shuffle(lines)
    num_val = int(len(lines) * VAL_SPLIT)
    num_train = len(lines) - num_val

    # Train with frozen layers first, to get a stable loss.
    # Adjust num epochs to your dataset. This step is enough to obtain a decent model.
    if True:
        model.compile(optimizer=Adam(lr=1e-3), loss={
            # use custom yolo_loss Lambda layer.
            'yolo_loss': lambda y_true, y_pred: y_pred})

        batch_size = 32
        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        history = model.fit_generator(data_generator_wrapper(lines[:num_train], batch_size, input_shape, anchors, num_classes),
                steps_per_epoch=max(1, num_train//batch_size),
                validation_data=data_generator_wrapper(lines[num_train:], batch_size, input_shape, anchors, num_classes),
                validation_steps=max(1, num_val//batch_size),
                epochs=epoch1,
                initial_epoch=0,
                callbacks=[logging, checkpoint])
        model.save_weights(os.path.join(LOG_DIR, 'trained_weights_stage_1.h5'))

        step1_train_loss = history.history['loss']
        with open(os.path.join(log_dir_time,'step1_loss.npy'), 'w') as f:
            for item in step1_train_loss:
                f.write("%s\n" % item) 

        step1_val_loss = np.array(history.history['val_loss'])
        with open(os.path.join(log_dir_time,'step1_val_loss.npy'), 'w') as f:
            for item in step1_val_loss:
                f.write("%s\n" % item) 

    # Unfreeze and continue training, to fine-tune.
    # Train longer if the result is not good.
    if True:
        for i in range(len(model.layers)):
            model.layers[i].trainable = True
        model.compile(optimizer=Adam(lr=1e-4), loss={'yolo_loss': lambda y_true, y_pred: y_pred}) # recompile to apply the change
        print('Unfreeze all layers.')

        batch_size = 4 # note that more GPU memory is required after unfreezing the body
        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        history=model.fit_generator(data_generator_wrapper(lines[:num_train], batch_size, input_shape, anchors, num_classes),
            steps_per_epoch=max(1, num_train//batch_size),
            validation_data=data_generator_wrapper(lines[num_train:], batch_size, input_shape, anchors, num_classes),
            validation_steps=max(1, num_val//batch_size),
            epochs=epoch1+epoch2,
            initial_epoch=epoch1,
            callbacks=[logging, checkpoint, reduce_lr, early_stopping])

        model.save_weights(os.path.join(LOG_DIR, 'trained_weights_final.h5'))

        step2_train_loss = history.history['loss']
        with open(os.path.join(log_dir_time,'step2_loss.npy'), 'w') as f:
            for item in step2_train_loss:
                f.write("%s\n" % item) 

        step2_val_loss = np.array(history.history['val_loss'])
        with open(os.path.join(log_dir_time,'step2_val_loss.npy'), 'w') as f:
            for item in step2_val_loss:
                f.write("%s\n" % item) 

# --- HELPER FUNCS

def get_classes(classes_path):
    """ loads the classes """
    with open(classes_path) as f:
        class_names = f.readlines()
    class_names = [c.strip() for c in class_names]
    return class_names

def get_anchors(anchors_path):
    '''loads the anchors from a file'''
    with open(anchors_path) as f:
        anchors = f.readline()
    anchors = [float(x) for x in anchors.split(',')]
    return np.array(anchors).reshape(-1, 2)

def create_model(input_shape, anchors, num_classes, load_pretrained=True, freeze_body=2,
            weights_path='keras_yolo3/model_data/yolo_weights.h5'):
    '''create the training model'''
    K.clear_session() # get a new session
    image_input = Input(shape=(None, None, 3))
    h, w = input_shape
    num_anchors = len(anchors)

    y_true = [Input(shape=(h//{0:32, 1:16, 2:8}[l], w//{0:32, 1:16, 2:8}[l], \
        num_anchors//3, num_classes+5)) for l in range(3)]

    model_body = yolo_body(image_input, num_anchors//3, num_classes)
    print('Create YOLOv3 model with {} anchors and {} classes.'.format(num_anchors, num_classes))

    if load_pretrained:
        model_body.load_weights(weights_path, by_name=True, skip_mismatch=True)
        print('Load weights {}.'.format(weights_path))
        if freeze_body in [1, 2]:
            # Freeze darknet53 body or freeze all but 3 output layers.
            num = (185, len(model_body.layers)-3)[freeze_body-1]
            for i in range(num): model_body.layers[i].trainable = False
            print('Freeze the first {} layers of total {} layers.'.format(num, len(model_body.layers)))

    model_loss = Lambda(yolo_loss, output_shape=(1,), name='yolo_loss',
        arguments={'anchors': anchors, 'num_classes': num_classes, 'ignore_thresh': 0.5})(
        [*model_body.output, *y_true])
    model = Model([model_body.input, *y_true], model_loss)

    return model

def data_generator(annotation_lines, batch_size, input_shape, anchors, num_classes):
    '''data generator for fit_generator'''
    n = len(annotation_lines)
    i = 0
    while True:
        image_data = []
        box_data = []
        for b in range(batch_size):
            if i==0:
                np.random.shuffle(annotation_lines)
            image, box = get_random_data(annotation_lines[i], input_shape, random=True)
            image_data.append(image)
            box_data.append(box)
            i = (i+1) % n
        image_data = np.array(image_data)
        box_data = np.array(box_data)
        y_true = preprocess_true_boxes(box_data, input_shape, anchors, num_classes)
        yield [image_data, *y_true], np.zeros(batch_size)

def data_generator_wrapper(annotation_lines, batch_size, input_shape, anchors, num_classes):
    n = len(annotation_lines)
    if n==0 or batch_size<=0: return None
    return data_generator(annotation_lines, batch_size, input_shape, anchors, num_classes)

# ----

if __name__ == '__main__':
    _main()

