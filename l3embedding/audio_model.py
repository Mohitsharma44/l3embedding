from keras.models import Model
from keras.layers import Input, Conv2D, BatchNormalization, MaxPooling2D, \
    MaxPooling1D, Flatten, Activation, Lambda, Reshape
from kapre.time_frequency import Spectrogram, Melspectrogram
import tensorflow as tf
from tensorflow.python.layers import base
import keras.regularizers as regularizers
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn
from tensorflow.python.layers import utils
from keras import backend as K
from tensorflow.python.framework import dtypes


## Modified from https://github.com/tensorflow/tensorflow/blob/r1.11/tensorflow/contrib/model_pruning/python/layers/core_layers.py

class MaskedConv2D(base.Layer):
  def __init__(self,
               threshold,
               rank,
               filters,
               kernel_size,
               strides=1,
               padding='valid',
               data_format='channels_last',
               dilation_rate=1,
               activation=None,
               use_bias=True,
               kernel_initializer=None,
               bias_initializer=init_ops.zeros_initializer(),
               kernel_regularizer=None,
               bias_regularizer=None,
               activity_regularizer=None,
               trainable=True,
               name=None,
               **kwargs):

    super(MaskedConv2D, self).__init__(trainable=trainable,
                                      name=name,
                                      activity_regularizer=activity_regularizer,
                                      **kwargs)

    self.rank = rank
    self.filters = filters
    self.kernel_size = utils.normalize_tuple(kernel_size, rank, 'kernel_size')
    self.strides = utils.normalize_tuple(strides, rank, 'strides')
    self.padding = utils.normalize_padding(padding)
    self.data_format = utils.normalize_data_format(data_format)
    self.dilation_rate = utils.normalize_tuple(dilation_rate, rank,
                                               'dilation_rate')
    self.activation = activation
    self.use_bias = use_bias
    self.kernel_initializer = kernel_initializer
    self.bias_initializer = bias_initializer
    self.kernel_regularizer = kernel_regularizer
    self.bias_regularizer = bias_regularizer
    self.input_spec = base.InputSpec(ndim=self.rank + 2)

  def build(self, input_shape):
    input_shape = tensor_shape.TensorShape(input_shape)
    channel_axis = 1 if self.data_format == 'channels_first' else -1
    if input_shape[channel_axis].value is None:
      raise ValueError('The channel dimension of the inputs '
                       'should be defined. Found `None`.')
    input_dim = input_shape[channel_axis].value
    kernel_shape = self.kernel_size + (input_dim, self.filters)
    self.mask = self.add_variable(name='mask',
                                  shape=kernel_shape,
                                  initializer=init_ops.ones_initializer(),
                                  trainable=False,
                                  dtype=self.dtype)

    self.kernel = self.add_variable(name='kernel',
                                    shape=kernel_shape,
                                    initializer=self.kernel_initializer,
                                    regularizer=self.kernel_regularizer,
                                    trainable=True,
                                    dtype=self.dtype)

    
    self.mask = K.cast(K.greater(K.abs(self.kernel), threshold), dtypes.float32)
    self.masked_kernel = math_ops.multiply(self.mask, self.kernel)

    if self.use_bias:
      self.bias = self.add_variable(name='bias',
                                    shape=(self.filters,),
                                    initializer=self.bias_initializer,
                                    regularizer=self.bias_regularizer,
                                    trainable=True,
                                    dtype=self.dtype)
    else:
      self.bias = None

    self.input_spec = base.InputSpec(
        ndim=self.rank + 2, axes={channel_axis: input_dim})
    self.built = True

  def call(self, inputs):
    outputs = nn.convolution(input=inputs,
                             filter=self.masked_kernel,
                             dilation_rate=self.dilation_rate,
                             strides=self.strides,
                             padding=self.padding.upper(),
                             data_format=utils.convert_data_format(self.data_format, self.rank + 2))

    if self.bias is not None:
      if self.data_format == 'channels_first':
        if self.rank == 1:
          # nn.bias_add does not accept a 1D input tensor.
          bias = array_ops.reshape(self.bias, (1, self.filters, 1))
          outputs += bias
        if self.rank == 2:
          outputs = nn.bias_add(outputs, self.bias, data_format='NCHW')
        if self.rank == 3:
          # As of Mar 2017, direct addition is significantly slower than
          # bias_add when computing gradients. To use bias_add, we collapse Z
          # and Y into a single dimension to obtain a 4D input tensor.
          outputs_shape = outputs.shape.as_list()
          outputs_4d = array_ops.reshape(outputs, [
              outputs_shape[0], outputs_shape[1],
              outputs_shape[2] * outputs_shape[3], outputs_shape[4]
          ])
          outputs_4d = nn.bias_add(outputs_4d, self.bias, data_format='NCHW')
          outputs = array_ops.reshape(outputs_4d, outputs_shape)
      else:
        outputs = nn.bias_add(outputs, self.bias, data_format='NHWC')

    if self.activation is not None:
      return self.activation(outputs)
    return outputs

  def compute_output_shape(self, input_shape):
    input_shape = tensor_shape.TensorShape(input_shape).as_list()
    if self.data_format == 'channels_last':
      space = input_shape[1:-1]
      new_space = []
      for i in range(len(space)):
        new_dim = utils.conv_output_length(space[i],
                                           self.kernel_size[i],
                                           padding=self.padding,
                                           stride=self.strides[i],
                                           dilation=self.dilation_rate[i])
        new_space.append(new_dim)
      return tensor_shape.TensorShape([input_shape[0]] + new_space + [self.filters])
    else:
      space = input_shape[2:]
      new_space = []
      for i in range(len(space)):
        new_dim = utils.conv_output_length(
            space[i],
            self.kernel_size[i],
            padding=self.padding,
            stride=self.strides[i],
            dilation=self.dilation_rate[i])
        new_space.append(new_dim)
      return tensor_shape.TensorShape([input_shape[0], self.filters] + new_space)
    
    

def construct_cnn_L3_orig_audio_model():
    """
    Constructs a model that replicates the audio subnetwork  used in Look,
    Listen and Learn

    Relja Arandjelovic and (2017). Look, Listen and Learn. CoRR, abs/1705.08168, .

    Returns
    -------
    model:  L3 CNN model
            (Type: keras.models.Model)
    inputs: Model inputs
            (Type: list[keras.layers.Input])
    outputs: Model outputs
            (Type: keras.layers.Layer)
    """
    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 512
    #n_win = 480
    #n_hop = n_win//2
    n_hop = 242
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # SPECTROGRAM PREPROCESSING
    # 257 x 199 x 1
    y_a = Spectrogram(n_dft=n_dft, n_hop=n_hop, power_spectrogram=1.0, # n_win=n_win,
                      return_decibel_spectrogram=False, padding='valid')(x_a)

    # Apply normalization from L3 paper
    y_a = Lambda(lambda x: tf.log(tf.maximum(x, 1e-12)) / 5.0)(y_a)

    # CONV BLOCK 1
    n_filter_a_1 = 64
    filt_size_a_1 = (3, 3)
    pool_size_a_1 = (2, 2)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_1, strides=2)(y_a)

    # CONV BLOCK 2
    n_filter_a_2 = 128
    filt_size_a_2 = (3, 3)
    pool_size_a_2 = (2, 2)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_2, strides=2)(y_a)

    # CONV BLOCK 3
    n_filter_a_3 = 256
    filt_size_a_3 = (3, 3)
    pool_size_a_3 = (2, 2)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_3, strides=2)(y_a)

    # CONV BLOCK 4
    n_filter_a_4 = 512
    filt_size_a_4 = (3, 3)
    pool_size_a_4 = (32, 24)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4,
                 kernel_initializer='he_normal',
                 name='audio_embedding_layer', padding='same',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_4)(y_a)

    y_a = Flatten()(y_a)

    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a


def construct_cnn_L3_kapredbinputbn_audio_model():
    """
    Constructs a model that replicates the audio subnetwork  used in Look,
    Listen and Learn

    Relja Arandjelovic and (2017). Look, Listen and Learn. CoRR, abs/1705.08168, .

    Returns
    -------
    model:  L3 CNN model
            (Type: keras.models.Model)
    inputs: Model inputs
            (Type: list[keras.layers.Input])
    outputs: Model outputs
            (Type: keras.layers.Layer)
    """
    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 512
    #n_win = 480
    #n_hop = n_win//2
    n_hop = 242
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # SPECTROGRAM PREPROCESSING
    # 257 x 199 x 1
    y_a = Spectrogram(n_dft=n_dft, n_hop=n_hop, power_spectrogram=1.0, # n_win=n_win,
                      return_decibel_spectrogram=True, padding='valid')(x_a)
    y_a = BatchNormalization()(y_a)

    # CONV BLOCK 1
    n_filter_a_1 = 64
    filt_size_a_1 = (3, 3)
    pool_size_a_1 = (2, 2)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_1, strides=2)(y_a)

    # CONV BLOCK 2
    n_filter_a_2 = 128
    filt_size_a_2 = (3, 3)
    pool_size_a_2 = (2, 2)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_2, strides=2)(y_a)

    # CONV BLOCK 3
    n_filter_a_3 = 256
    filt_size_a_3 = (3, 3)
    pool_size_a_3 = (2, 2)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_3, strides=2)(y_a)

    # CONV BLOCK 4
    n_filter_a_4 = 512
    filt_size_a_4 = (3, 3)
    pool_size_a_4 = (32, 24)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4,
                 kernel_initializer='he_normal',
                 name='audio_embedding_layer', padding='same',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_4)(y_a)

    y_a = Flatten()(y_a)

    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a

def construct_cnn_L3_melspec1_audio_model():
    """
    Constructs a model that replicates the audio subnetwork  used in Look,
    Listen and Learn

    Relja Arandjelovic and (2017). Look, Listen and Learn. CoRR, abs/1705.08168, .

    Returns
    -------
    model:  L3 CNN model
            (Type: keras.models.Model)
    inputs: Model inputs
            (Type: list[keras.layers.Input])
    outputs: Model outputs
            (Type: keras.layers.Layer)
    """
    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 2048
    #n_win = 480
    #n_hop = n_win//2
    n_mels = 128
    n_hop = 242
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # MELSPECTROGRAM PREPROCESSING
    # 128 x 199 x 1
    y_a = Melspectrogram(n_dft=n_dft, n_hop=n_hop, n_mels=n_mels,
                      sr=asr, power_melgram=1.0, htk=True, # n_win=n_win,
                      return_decibel_melgram=True, padding='same')(x_a)
    #y_a = BatchNormalization()(y_a)

    # CONV BLOCK 1
    n_filter_a_1 = 64
    filt_size_a_1 = (3, 3)
    pool_size_a_1 = (2, 2)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_1, strides=2)(y_a)

    # CONV BLOCK 2
    n_filter_a_2 = 128
    filt_size_a_2 = (3, 3)
    pool_size_a_2 = (2, 2)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_2, strides=2)(y_a)

    # CONV BLOCK 3
    n_filter_a_3 = 256
    filt_size_a_3 = (3, 3)
    pool_size_a_3 = (2, 2)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_3, strides=2)(y_a)

    # CONV BLOCK 4
    n_filter_a_4 = 512
    filt_size_a_4 = (3, 3)
    pool_size_a_4 = (16, 24)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4,
                 kernel_initializer='he_normal',
                 name='audio_embedding_layer', padding='same',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_4)(y_a)

    y_a = Flatten()(y_a)

    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a


def construct_cnn_L3_melspec2_audio_model_multiGPU(thresholds):
    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 2048
    #n_win = 480
    #n_hop = n_win//2
    n_mels = 256
    n_hop = 242
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # MELSPECTROGRAM PREPROCESSING
    # 128 x 199 x 1
    y_a = Melspectrogram(n_dft=n_dft, n_hop=n_hop, n_mels=n_mels,
                         sr=asr, power_melgram=1.0, htk=True, # n_win=n_win,
                         return_decibel_melgram=True, padding='same')(x_a)
    y_a = BatchNormalization()(y_a)

    # CONV BLOCK 1
    n_filter_a_1 = 64
    filt_size_a_1 = (3, 3)
    pool_size_a_1 = (2, 2)
    y_a = MaskedConv2D(thresholds['conv_1'], 2, n_filter_a_1, filt_size_a_1, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaskedConv2D(thresholds['conv_2'], 2, n_filter_a_1, filt_size_a_1, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_1, strides=2)(y_a)

    # CONV BLOCK 2
    n_filter_a_2 = 128
    filt_size_a_2 = (3, 3)
    pool_size_a_2 = (2, 2)
    y_a = MaskedConv2D(thresholds['conv_3'],2, n_filter_a_2, filt_size_a_2, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaskedConv2D(thresholds['conv_4'], 2, n_filter_a_2, filt_size_a_2, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_2, strides=2)(y_a)

    # CONV BLOCK 3
    n_filter_a_3 = 256
    filt_size_a_3 = (3, 3)
    pool_size_a_3 = (2, 2)
    y_a = MaskedConv2D(thresholds['conv_5'], 2, n_filter_a_3, filt_size_a_3, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaskedConv2D(thresholds['conv_6'], 2, n_filter_a_3, filt_size_a_3, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_3, strides=2)(y_a)

    # CONV BLOCK 4
    n_filter_a_4 = 512
    filt_size_a_4 = (3, 3)
    pool_size_a_4 = (32, 24)
    y_a = MaskedConv2D(thresholds['conv_7'], 2, n_filter_a_4, filt_size_a_4, padding='same',
                       kernel_initializer='he_normal',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaskedConv2D(thresholds['conv_8'], 2, n_filter_a_4, filt_size_a_4,
                       kernel_initializer='he_normal',
                       name='audio_embedding_layer', padding='same',
                       kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_4)(y_a)

    y_a = Flatten()(y_a)

    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a

def construct_cnn_L3_melspec2_kd_audio_model(masks):
    
    def get_masked_output(y, masks, layer_name):
        out = nn.convolution(input=tf.ones_like(y), filter=masks[layer_name], padding='SAME', data_format='NHWC')
        out = K.cast(K.greater(K.abs(out), 0.0), dtypes.float32)
        return out

    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 2048
    #n_win = 480
    #n_hop = n_win//2
    n_mels = 256
    n_hop = 242
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # MELSPECTROGRAM PREPROCESSING
    # 128 x 199 x 1
    y_a = Melspectrogram(n_dft=n_dft, n_hop=n_hop, n_mels=n_mels,
                      sr=asr, power_melgram=1.0, htk=True, # n_win=n_win,
                      return_decibel_melgram=True, padding='same')(x_a)
    y_a = BatchNormalization()(y_a)

    # CONV BLOCK 1
    n_filter_a_1 = 64
    filt_size_a_1 = (3, 3)
    pool_size_a_1 = (2, 2)

    #y_a_1 = get_masked_output(y_a, masks, 'conv_1')

    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_1',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    #y_a = Lambda(lambda x: math_ops.multiply(y_a_1, x), trainable=False)(y_a)

    y_a_2 = get_masked_output(y_a, masks, 'conv_2')

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_2',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    
    y_a = Lambda(lambda x: math_ops.multiply(y_a_2, x), trainable=False)(y_a)

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_1, strides=2)(y_a)

    # CONV BLOCK 2
    n_filter_a_2 = 128
    filt_size_a_2 = (3, 3)
    pool_size_a_2 = (2, 2)
    y_a_3 = get_masked_output(y_a, masks, 'conv_3')

    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_3',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    y_a = Lambda(lambda x: math_ops.multiply(y_a_3, x), trainable=False)(y_a)

    y_a_4 = get_masked_output(y_a, masks, 'conv_4')

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_4',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    y_a = Lambda(lambda x: math_ops.multiply(y_a_4, x), trainable=False)(y_a)

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_2, strides=2)(y_a)

    # CONV BLOCK 3
    n_filter_a_3 = 256
    filt_size_a_3 = (3, 3)
    pool_size_a_3 = (2, 2)

    y_a_5 = get_masked_output(y_a, masks, 'conv_5')

    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_5',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    y_a = Lambda(lambda x: math_ops.multiply(y_a_5, x), trainable=False)(y_a)

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)

    y_a_6 = get_masked_output(y_a, masks, 'conv_6')

    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_6',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    y_a = Lambda(lambda x: math_ops.multiply(y_a_6, x), trainable=False)(y_a)

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_3, strides=2)(y_a)

    # CONV BLOCK 4
    n_filter_a_4 = 512
    filt_size_a_4 = (3, 3)
    pool_size_a_4 = (32, 24)

    y_a_7 = get_masked_output(y_a, masks, 'conv_7')

    y_a = Conv2D(n_filter_a_4, filt_size_a_4, padding='same',
                 kernel_initializer='he_normal',
                 name='conv_7',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)

    y_a = Lambda(lambda x: math_ops.multiply(y_a_7, x), trainable=False)(y_a)
        
    y_a_8 = get_masked_output(y_a, masks, 'conv_8')

    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4,
                 kernel_initializer='he_normal',
                 name='audio_embedding_layer', padding='same',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    
    y_a = Lambda(lambda x: math_ops.multiply(y_a_8, x), trainable=False)(y_a)
    
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_4)(y_a)

    y_a = Flatten()(y_a)

    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a


def construct_cnn_L3_melspec2_audio_model():
    """
    Constructs a model that replicates the audio subnetwork  used in Look,
    Listen and Learn

    Relja Arandjelovic and (2017). Look, Listen and Learn. CoRR, abs/1705.08168, .

    Returns
    -------
    model:  L3 CNN model
            (Type: keras.models.Model)
    inputs: Model inputs
            (Type: list[keras.layers.Input])
    outputs: Model outputs
            (Type: keras.layers.Layer)
    """
    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 2048
    #n_win = 480
    #n_hop = n_win//2
    n_mels = 256
    n_hop = 242
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # MELSPECTROGRAM PREPROCESSING
    # 128 x 199 x 1
    y_a = Melspectrogram(n_dft=n_dft, n_hop=n_hop, n_mels=n_mels,
                      sr=asr, power_melgram=1.0, htk=True, # n_win=n_win,
                      return_decibel_melgram=True, padding='same')(x_a)
    y_a = BatchNormalization()(y_a)

    # CONV BLOCK 1
    n_filter_a_1 = 64
    filt_size_a_1 = (3, 3)
    pool_size_a_1 = (2, 2)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_1, filt_size_a_1, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_1, strides=2)(y_a)

    # CONV BLOCK 2
    n_filter_a_2 = 128
    filt_size_a_2 = (3, 3)
    pool_size_a_2 = (2, 2)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_2, filt_size_a_2, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_2, strides=2)(y_a)

    # CONV BLOCK 3
    n_filter_a_3 = 256
    filt_size_a_3 = (3, 3)
    pool_size_a_3 = (2, 2)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_3, filt_size_a_3, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_3, strides=2)(y_a)

    # CONV BLOCK 4
    n_filter_a_4 = 512
    filt_size_a_4 = (3, 3)
    pool_size_a_4 = (32, 24)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4, padding='same',
                 kernel_initializer='he_normal',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = Conv2D(n_filter_a_4, filt_size_a_4,
                 kernel_initializer='he_normal',
                 name='audio_embedding_layer', padding='same',
                 kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=pool_size_a_4)(y_a)

    y_a = Flatten()(y_a)

    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a


def convert_audio_model_to_embedding(audio_model, x_a, model_type, pooling_type='original', kd_model=False):
    """
    Given and audio subnetwork, return a model that produces the learned
    embedding

    Args:
        audio_model: audio subnetwork
        x_a: audio data input Tensor
        model_type: the model type string

    Returns:
        m: Model object
        x_a : audio data input Tensor
        y_a: embedding output Tensor
    """

    pooling = {
        'cnn_L3_orig': {
            'original': (8, 8),
            'short': (32, 24),
        },
        'cnn_L3_kapredbinputbn': {
            'original': (8, 8),
            'short': (32, 24),
        },
        'cnn_L3_melspec1': {
            'original': (4, 8),
            'short': (16, 24),
        },
        'cnn_L3_melspec2': {
            'original': (8, 8),
            'short': (32, 24),
            'kd_256': 2,
            'kd_128': 4,
        },
        'cnn_L3_melspec2_audioonly': {
            'original': (8, 8),
            'short': (32, 24),
            'kd_256': 2,
            'kd_128': 4,
        }
    }
    
    
    if kd_model:
        pool_size = pooling[model_type]['short']
        embedding_pool = pooling[model_type][pooling_type]
    else:
        pool_size = pooling[model_type][pooling_type]

    embed_layer = audio_model.get_layer('audio_embedding_layer')
    y_a = MaxPooling2D(pool_size=pool_size, padding='same')(embed_layer.output)
    
    if kd_model:
        y_a = Reshape((y_a.shape[3], 1))(y_a)
        y_a = MaxPooling1D(pool_size=embedding_pool, border_mode='valid')(y_a)
        
    y_a = Flatten()(y_a)
    m = Model(inputs=x_a, outputs=y_a)
    
    return m, x_a, y_a


def construct_tiny_L3_audio_model():
    """
    Constructs a model that implements a small L3 audio subnetwork

    Returns
    -------
    model:  L3 CNN model
            (Type: keras.models.Model)
    inputs: Model inputs
            (Type: list[keras.layers.Input])
    outputs: Model outputs
            (Type: keras.layers.Layer)
    """
    weight_decay = 1e-5
    ####
    # Audio subnetwork
    ####
    n_dft = 512
    n_win = 480
    n_hop = n_win//2
    asr = 48000
    audio_window_dur = 1
    # INPUT
    x_a = Input(shape=(1, asr * audio_window_dur), dtype='float32')

    # SPECTROGRAM PREPROCESSING
    y_a = Spectrogram(n_dft=n_dft, n_win=n_win, n_hop=n_hop,
                      return_decibel_spectrogram=True, padding='valid')(x_a)

    y_a = Conv2D(10, (5,5), padding='valid', strides=(1,1),
                 kernel_initializer='he_normal',
                         kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=(3,3), strides=3)(y_a)
    y_a = Conv2D(10, (5,5), padding='valid', strides=(1,1),
                 kernel_initializer='he_normal',
                         kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=(3,3), strides=3)(y_a)
    y_a = Conv2D(10, (5,5), padding='valid', strides=(1,1),
                 kernel_initializer='he_normal',
                         kernel_regularizer=regularizers.l2(weight_decay))(y_a)
    y_a = BatchNormalization()(y_a)
    y_a = Activation('relu')(y_a)
    y_a = MaxPooling2D(pool_size=(3,3), strides=3)(y_a)
    y_a = Flatten(name='embedding')(y_a)
    m = Model(inputs=x_a, outputs=y_a)
    m.name = 'audio_model'

    return m, x_a, y_a
