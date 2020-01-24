# Copyright (c) 2019 Horizon Robotics. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Actor and Value networks for target navigation task."""

import collections
import gin
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from scipy.misc import imresize
import tensorflow as tf
import tf_agents
from tf_agents.networks.actor_distribution_rnn_network import ActorDistributionRnnNetwork
from tf_agents.networks.value_rnn_network import ValueRnnNetwork
from tf_agents.networks.actor_distribution_network import ActorDistributionNetwork
from tf_agents.networks.value_network import ValueNetwork

from alf.algorithms.agent import get_obs
from alf.utils import common
from alf.layers import get_identity_layer, get_first_element_layer


# followed https://www.tensorflow.org/guide/keras/custom_layers_and_models
class ImageLanguageAttentionCombiner(tf.keras.layers.Layer):
    def __init__(self,
                 vocab_size,
                 n_heads=1,
                 network_to_debug=None,
                 output_attention_max=False,
                 **kwargs):
        super().__init__(**kwargs)
        self._vocab_size = vocab_size
        self._n_heads = n_heads
        self._network_to_debug = network_to_debug
        self._output_attention_max = output_attention_max
        self.fig = None

    def build(self, input_shape):
        assert isinstance(input_shape, list)
        key_shape = input_shape[0]
        query_shape = input_shape[1]
        states_shape = 0
        if len(input_shape) > 2:
            states_shape = input_shape[2]
        (b, h, w, c) = key_shape  # channels last
        self._embedding = tf.keras.layers.Embedding(self._vocab_size, c)
        self._embedding.build(input_shape[1])

        self._attention = tf.keras.layers.Attention(use_scale=True)
        query_emb_shape = self._embedding.compute_output_shape(query_shape)
        key_value_shape = (b, h * w, c + c)
        key_emb_shape = (b, h * w, c)
        self._attention.build(
            input_shape=[query_emb_shape, key_value_shape, key_emb_shape])
        super().build(input_shape)

    # def compute_output_shape(self, input_shape):
    #     assert isinstance(input_shape, list)
    #     key_shape = input_shape[0]
    #     states_shape = 0
    #     if len(input_shape) > 2:
    #         states_shape = input_shape[2]
    #     b = key_shape[0]
    #     c = key_shape[-1]  # channels last
    #     # Output per batch contains query: c num_embedding_dims, image: c channels, position: 2 dims
    #     output_channels = 3 * c  # 2 * c + 2 if not tiling position tensor
    #     if self._output_attention_max:
    #         output_channels += c
    #     return (b, output_channels)

    def get_config(self):
        config = super().get_config()
        config.update({
            'vocab_size': self._vocab_size,
            'n_heads': self._n_heads,
            'output_attention_max': self._output_attention_max,
            'network_to_debug': self._network_to_debug
        })
        return config

    def call(self, inputs):
        """
        Generate the attention layer output

        Args:
            input: a list of two tensors:
                    image: the output of the conv_layers, which will be flattened
                        into tensor of shape (batch, h * w, channels),
                    sentence: integer encoded raw sentence input of shape (batch,
                        seq_length -- 20 from PlayGround.vocab_sequence_length),
                        which will be embedded into shape (batch, seq_length,
                        num_embedding_dim), where num_embedding_dim == channels
                        from the image input (key) in order to do inner product in
                        attention layer.
        """
        assert isinstance(inputs, list)
        key = inputs[0]
        query = inputs[1]
        states = None
        if len(inputs) > 2:
            states = inputs[2]

        # flatten image tensor [batches, height, width, channels]
        # into [batches, height * width, channels].
        # Assumes channels_last.
        (b, h, w, c) = key.shape
        flatten_shape = (b, h * w, c)
        key_embeddings = tf.reshape(key, flatten_shape)

        # create position input tensor
        x = tf.reshape(
            tf.constant(np.asarray(range(w)), dtype=tf.float32), (1, w))
        x = tf.reshape(tf.keras.backend.repeat(x, n=h), (h * w, 1))
        y = tf.reshape(
            tf.constant(np.asarray(range(h)), dtype=tf.float32), (1, h))
        y = tf.reshape(
            tf.transpose(tf.keras.backend.repeat(y, n=w)), (h * w, 1))
        p = tf.concat([x, y], -1)  # (h * w, 2)
        p = tf.keras.backend.repeat(tf.reshape(p, (h * w * 2, 1)), n=b)
        p = tf.transpose(p)
        p = tf.reshape(p,
                       (b, h * w, 2)) / w  # divide by w to normalize input pos

        # value: (b, h*w, 2 * c/2)
        pos_embeddings = tf.tile(p, multiples=[1, 1, int(c / 2)])
        # inner product (Luong style) attention

        # c will be the number of embedding dimensions for the sentence input,
        # and num_embedding_dims parameter is ignored.  This is because we do
        # inner product of image and sentence embedding vectors to compute attention.
        # query_embeddings shape: (seq_len, c)
        query_embeddings = self._embedding(query)

        key_value = tf.concat([key_embeddings, pos_embeddings], axis=-1)
        n_heads = self._n_heads
        nbatch = b

        # Section of code adapted from https://github.com/google/trax/blob/master/trax/layers/attention.py
        # under http://www.apache.org/licenses/LICENSE-2.0

        # nbatch, seqlen, d_feature --> nbatch, n_heads, seqlen, d_head
        def SplitHeads(x, d_feature):
            assert d_feature % n_heads == 0
            d_head = d_feature // n_heads
            return tf.transpose(
                tf.reshape(x, (nbatch, -1, n_heads, d_head)), (0, 2, 1, 3))

        # nbatch, n_heads, seqlen, d_head --> nbatch, seqlen, d_feature
        def JoinHeads(x, d_feature):  # pylint: disable=invalid-name
            assert d_feature % n_heads == 0
            d_head = d_feature // n_heads
            return tf.reshape(
                tf.transpose(x, (0, 2, 1, 3)), (nbatch, -1, n_heads * d_head))

        # End section of code

        orig_query_embeddings = query_embeddings
        if n_heads > 1:
            query_embeddings = SplitHeads(query_embeddings, c)
            key_value = SplitHeads(key_value, 2 * c)
            key_embeddings = SplitHeads(key_embeddings, c)

        # print('query ', query_embeddings, '\nkey_val ', key_value, '\nkey ',
        #       key_embeddings)

        # generates the position attention of shape (batch, seq_len, c+c)  or c+2 if not tiling
        pos_attention_seq = self._attention(
            [query_embeddings, key_value, key_embeddings])

        if n_heads > 1:
            pos_attention_seq = JoinHeads(pos_attention_seq, 2 * c)

        attn_scores = tf.matmul(
            query_embeddings, key_embeddings, transpose_b=True)
        # print('attn_scores ', attn_scores)
        if n_heads > 1:
            attn_scores = tf.reduce_mean(attn_scores, axis=2)
        else:
            attn_scores = tf.keras.layers.GlobalAveragePooling1D()(
                attn_scores)  # across seq_len
        attn_score_max = tf.reduce_max(
            tf.nn.softmax(attn_scores), axis=-1)  # across h*w
        if tf.executing_eagerly():
            # shape (batch, )
            tf.summary.histogram(
                name=self.name + "/attention/value-max", data=attn_score_max)

            if self._network_to_debug == self.name:
                # take first batch
                attn = tf.reshape(attn_scores, (b, n_heads, h, w))[0]

                def tensor_to_image(attn, img):
                    # Default color palette: viridis (purple to yellow):
                    # https://cran.r-project.org/web/packages/viridis/vignettes/intro-to-viridis.html
                    if 0:  # Smaller image:
                        img = imresize(img, attn.shape, interp='bilinear')
                        attn = np.repeat(
                            np.reshape(attn, (h, w, 1)), 3, axis=-1)
                    else:  # Larger image:
                        attn = np.repeat(
                            np.reshape(attn, (h, w, 1)), 3, axis=-1)
                        attn = imresize(
                            attn * 255, img.shape, interp='nearest') / 255.
                        attn = np.clip(attn, 0, 1)
                    attn_img = (img * attn).astype(np.uint8)
                    return attn_img

                img = get_obs()['image'].numpy()  # will fail in graph mode
                img = img[
                    0, :, :,
                    -3:] * 255.  # take first batch, take last 3 channels to revert FrameStack, multiply by 255 to revert image scale wrapper
                img = np.clip(img, 0, 255)
                num_rows = 2
                for i in range(n_heads):
                    attn_img = tensor_to_image(attn[i].numpy(), img)
                    if self.fig is None:
                        plt.interactive(True)
                        _, axs = plt.subplots(num_rows,
                                              int(n_heads / num_rows))
                        self.fig = axs
                        plt.show()
                    self.fig[i % num_rows, int(i / num_rows)].imshow(attn_img)
                # plt.pause(.00001)
                input()

        query_encoding = tf.keras.layers.GlobalAveragePooling1D()(
            orig_query_embeddings)
        pos_attention = tf.keras.layers.GlobalAveragePooling1D()(
            pos_attention_seq)
        # tf.print(self.name, " pos: ", pos_attention[0][-4:], " attn_max", attn_score_max)

        outputs = [query_encoding, pos_attention]
        if self._output_attention_max:
            outputs.append(
                tf.reshape(
                    tf.keras.backend.repeat(
                        tf.reshape(attn_score_max, (b, n_heads)), n=c),
                    (b, c * n_heads)))
        if states is not None:
            outputs.append(states)

        return tf.keras.layers.Concatenate()(outputs)


@gin.configurable
def get_ac_networks(conv_layer_params=None,
                    attention=False,
                    activation_fn=tf.keras.activations.softsign,
                    kernel_initializer=tf.keras.initializers.GlorotUniform(),
                    num_embedding_dims=None,
                    fc_layer_params=None,
                    num_state_tiles=None,
                    num_sentence_tiles=None,
                    name=None,
                    n_heads=1,
                    network_to_debug=None,
                    output_attention_max=False,
                    rnn=True):
    """
    Generate the actor and value networks

    Args:
        conv_layer_params (list[int 3 tuple]): optional convolution layers
            parameters, where each item is a length-three tuple indicating
            (filters, kernel_size, stride).
        attention (bool): if true, use attention after conv layer.
            Assumes that sentence is part of the input dictionary.
        num_embedding_dims (int): optional number of dimensions of the
            vocabulary embedding space.
        fc_layer_params (list[int]): optional fully_connected parameters, where
            each item is the number of units in the layer.
        num_state_tiles (int): optional number of times to repeat the
            internal state tensor before concatenation with other inputs.
            The rationale is to match the number of dimentions of the image
            input, so that the final concatenation will have roughly equal
            representation from different sources of input.  Without this,
            typically image input, due to its large input size, will take over
            and trump all other small dimensional inputs.
        num_sentence_tiles (int): optional number of times to repeat the
            sentence embedding tensor before concatenation with other inputs,
            so that sentence input won't be trumped by other high dimensional
            inputs like image observation.
    """
    observation_spec = common.get_observation_spec()
    action_spec = common.get_action_spec()

    if attention:
        num_embedding_dims = conv_layer_params[-1][0]

    vocab_size = common.get_vocab_size()
    if vocab_size:
        if not attention:
            sentence_layers = tf.keras.Sequential([
                tf.keras.layers.Embedding(vocab_size, num_embedding_dims),
                tf.keras.layers.GlobalAveragePooling1D()
            ])
            if num_sentence_tiles:
                sentence_layers.add(
                    tf.keras.layers.Lambda(lambda x: tf.tile(
                        x, multiples=[1, num_sentence_tiles])))
        else:  # attention
            sentence_layers = get_identity_layer()

    # image:
    conv_layers = []
    conv_layers.extend([
        tf.keras.layers.Conv2D(
            filters=filters,
            kernel_size=kernel_size,
            strides=strides,
            activation=activation_fn,
            kernel_initializer=kernel_initializer,
            name='/'.join([name, 'conv2d']) if name else None)
        for (filters, kernel_size, strides) in conv_layer_params
    ])

    image_layers = conv_layers
    if not attention:
        image_layers.append(tf.keras.layers.Flatten())
    image_layers = tf.keras.Sequential(image_layers)

    preprocessing_layers = {}
    obs_spec = common.get_observation_spec()
    if isinstance(obs_spec, dict) and 'image' in obs_spec:
        preprocessing_layers['image'] = image_layers

    if isinstance(obs_spec, dict) and 'sentence' in obs_spec:
        preprocessing_layers['sentence'] = sentence_layers

    if isinstance(obs_spec, dict) and 'states' in obs_spec:
        state_layers = get_identity_layer()
        # [image: (1, 12800), sentence: (1, 16 * 800), states: (1, 16 * 800)]
        # Here, we tile along the last dimension of the input.
        if num_state_tiles:
            state_layers = tf.keras.Sequential([
                tf.keras.layers.Lambda(lambda x: tf.tile(
                    x, multiples=[1, num_state_tiles]))
            ])
        preprocessing_layers['states'] = state_layers

    # This makes the code work with internal states input alone as well.
    if not preprocessing_layers:
        preprocessing_layers = get_identity_layer()

    preprocessing_combiner = tf.keras.layers.Concatenate()

    if attention:
        attention_combiner = ImageLanguageAttentionCombiner(
            vocab_size=vocab_size,
            n_heads=n_heads,
            name=name,
            network_to_debug=network_to_debug,
            output_attention_max=output_attention_max)
        preprocessing_combiner = attention_combiner

    if not isinstance(preprocessing_layers, dict):
        preprocessing_combiner = None

    if name == 'actor':
        print('==========================================')
        print('observation_spec: ', observation_spec)
        print('action_spec: ', action_spec)

    if rnn:
        actor = ActorDistributionRnnNetwork(
            input_tensor_spec=observation_spec,
            output_tensor_spec=action_spec,
            preprocessing_layers=preprocessing_layers,
            preprocessing_combiner=preprocessing_combiner,
            input_fc_layer_params=fc_layer_params)

        value = ValueRnnNetwork(
            input_tensor_spec=observation_spec,
            preprocessing_layers=preprocessing_layers,
            preprocessing_combiner=preprocessing_combiner,
            input_fc_layer_params=fc_layer_params)
    else:
        actor = ActorDistributionNetwork(
            input_tensor_spec=observation_spec,
            output_tensor_spec=action_spec,
            preprocessing_layers=preprocessing_layers,
            preprocessing_combiner=preprocessing_combiner,
            fc_layer_params=fc_layer_params)

        value = ValueNetwork(
            input_tensor_spec=observation_spec,
            preprocessing_layers=preprocessing_layers,
            preprocessing_combiner=preprocessing_combiner,
            fc_layer_params=fc_layer_params)

    return actor, value


@gin.configurable
def get_actor_network(conv_layer_params=None,
                      activation_fn=tf.keras.activations.softsign,
                      kernel_initializer=tf.keras.initializers.GlorotUniform(),
                      num_embedding_dims=None,
                      fc_layer_params=None,
                      num_state_tiles=None,
                      num_sentence_tiles=None):
    """
    Generate the actor network
    """
    a, _ = get_ac_networks(
        conv_layer_params=conv_layer_params,
        activation_fn=activation_fn,
        kernel_initializer=kernel_initializer,
        num_embedding_dims=num_embedding_dims,
        fc_layer_params=fc_layer_params,
        num_state_tiles=num_state_tiles,
        num_sentence_tiles=num_sentence_tiles,
        name="actor")
    return a


@gin.configurable
def get_value_network(conv_layer_params=None,
                      activation_fn=tf.keras.activations.softsign,
                      kernel_initializer=tf.keras.initializers.GlorotUniform(),
                      num_embedding_dims=None,
                      fc_layer_params=None,
                      num_state_tiles=None,
                      num_sentence_tiles=None):
    """
    Generate the value network
    """
    _, c = get_ac_networks(
        conv_layer_params=conv_layer_params,
        activation_fn=activation_fn,
        kernel_initializer=kernel_initializer,
        num_embedding_dims=num_embedding_dims,
        fc_layer_params=fc_layer_params,
        num_state_tiles=num_state_tiles,
        num_sentence_tiles=num_sentence_tiles,
        name="value")
    return c
