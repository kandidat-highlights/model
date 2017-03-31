# MIT License
#
# Copyright (c) 2017 Jonatan Almén, Alexander Håkansson, Jesper Jaxing, Gmal
# Tchaefa, Maxim Goretskyy, Axel Olivecrona
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# ==============================================================================
""" A RNN model for predicting which people would like a text

The model is created as part of the bachelor's thesis
"Automatic mentions and highlights" at Chalmers University of
Technology and the University of Gothenburg.
"""
import glob
import os.path
import tensorflow as tf
from definitions import CHECKPOINTS_DIR, TENSOR_DIR_VALID, TENSOR_DIR_TRAIN
from .util import data as data
from .util.folder_builder import build_structure
from .util.writer import log_config


# TODO Separera checkpoints ut ur modell klassen
class Model(object):
    def __init__(self, config, session):
        self._session = session
        self.output_layer = None
        self.latest_layer = None
        self.output_weights = None
        self.output_bias = None
        self.l2_term = tf.constant(0, dtype=tf.float64)

        self.vocabulary_size = config['vocabulary_size']
        self.user_count = config['user_count']
        self.learning_rate = config['learning_rate']
        self.embedding_size = config['embedding_size']
        self.max_title_length = config['max_title_length']
        self.lstm_neurons = config['lstm_neurons']
        self.batch_size = config['batch_size']
        self.training_epochs = config['training_epochs']
        self.use_l2_loss = config['use_l2_loss']
        self.l2_factor = config['l2_factor']
        self.use_dropout = config['use_dropout']
        self.dropout_prob = config['dropout_prob'] # Only used for train op
        self.hidden_layers = config['hidden_layers']
        self.hidden_neurons = config['hidden_neurons']
        self.is_trainable_matrix = config['trainable_matrix']
        self.use_pretrained = config['use_pretrained']
        self.use_constant_limit = config['use_constant_limit']
        self.constant_prediction_limit = config['constant_prediction_limit']
        self.use_concat_input = config['use_concat_input']

        # Will be set in build_graph
        self.input = None
        self.subreddit_input = None
        self.target = None
        self.sigmoid = None
        self.train_op = None
        self.error = None
        self.init_op = None
        self.saver = None
        self.epoch = None
        self.keep_prob = None
        self.embedding_placeholder = None
        self.embedding_init = None
        self.train_writer = None
        self.valid_writer = None

        # variables for tensorboard
        self.prec_sum_training = None
        self.error_sum = None
        self.recall_sum_training = None
        self.f1_sum_training = None
        self.prec_sum_validation = None
        self.error_sum = None
        self.recall_sum_validation = None
        self.f1_sum_validation = None

        self.logging_dir = build_structure(config)
        self.checkpoints_dir = self.logging_dir + '/' + CHECKPOINTS_DIR + '/' + "models.ckpt"
        log_config(config) #Discuss if we should do this after, and somehow take "highest" precision from validation?

        with tf.device("/cpu:0"):
            self.data = data.Data(config)
            if self.use_pretrained:
                self.vocabulary_size = len(self.data.embedding_matrix)


    def load_checkpoint(self):
        """ Loads any exisiting trained model """
        checkpoint_files = glob.glob(self.checkpoints_dir + "*")
        if all([os.path.isfile(file) for file in checkpoint_files]) \
                and checkpoint_files:
            self.saver.restore(self._session, self.checkpoints_dir)
            self._session.run(tf.local_variables_initializer())
        else:
            self._session.run(self.init_op)

    def save_checkpoint(self, path=None):
        """ Saves the model to a file """
        path = path or self.checkpoints_dir
        self.saver.save(self._session, path)

    def validate(self):
        """ Validates the model and returns the final precision """
        print("Starting validation...")
        # Evaluate epoch
        epoch = self.epoch.eval(self._session)

        # Compute validation error
        val_data, val_sub, val_labels = self.data.get_validation()
        val_prec, val_err, val_recall, val_f1 = \
            self._session.run([self.prec_sum_validation,
                               self.error_sum,
                               self.recall_sum_validation,
                               self.f1_sum_validation],
                              {self.input: val_data,
                               self.subreddit_input: val_sub,
                               self.target: val_labels,
                               self.keep_prob: 1.0})

        # Write results to TensorBoard
        self.valid_writer.add_summary(val_prec, epoch)
        self.valid_writer.add_summary(val_err, epoch)
        self.valid_writer.add_summary(val_recall, epoch)
        self.valid_writer.add_summary(val_f1, epoch)

        # Compute training error
        train_data, train_sub, train_labels = self.data.get_training()
        train_prec, train_err, train_recall, train_f1 = \
            self._session.run([self.prec_sum_training,
                               self.error_sum,
                               self.recall_sum_training,
                               self.f1_sum_training],
                              {self.input: train_data,
                               self.subreddit_input: train_sub,
                               self.target: train_labels,
                               self.keep_prob: 1.0})
        # Write results to Tensorboard
        self.train_writer.add_summary(train_prec, epoch)
        self.train_writer.add_summary(train_err, epoch)
        self.train_writer.add_summary(train_recall, epoch)
        self.train_writer.add_summary(train_f1, epoch)

    def validate_batch(self):
        """ Validates a batch of data and returns cross entropy error """
        with tf.device("/cpu:0"):
            batch_input, batch_sub, batch_label = self.data.next_valid_batch()

        return self._session.run(self.error,
                                 feed_dict={self.input: batch_input,
                                            self.subreddit_input: batch_sub,
                                            self.target: batch_label})

    # TODO funktionen gör alldeles för mycket,
    # dela upp utskrift, beräkning och träning
    def train(self):
        """ Trains the model on the dataset """
        print("Starting training...")

        if self.use_pretrained:
            self._session.run(self.embedding_init,
                              feed_dict={self.embedding_placeholder:
                                         self.data.embedding_matrix})
        self.train_writer = \
            tf.summary.FileWriter(self.logging_dir + '/' + TENSOR_DIR_TRAIN,
                                  self._session.graph)
        self.valid_writer = \
            tf.summary.FileWriter(self.logging_dir + '/' + TENSOR_DIR_VALID)

        old_epoch = 0

        if self.epoch.eval(self._session) == 0:
            self.validate()

        # Train for a specified amount of epochs
        for i in self.data.for_n_train_epochs(self.training_epochs,
                                              self.batch_size):
            # Debug print out
            epoch = self.data.completed_training_epochs
            training_error = self.train_batch()
            validation_error = self.validate_batch()

            # Don't validate so often
            if i % (self.data.train_size // self.batch_size // 10) == 0 and i:
                done = self.data.percent_of_epoch
                print("Validation error: {:f} | Training error: {:f} | Done: {:.0%}"
                      .format(validation_error, training_error, done))

            # Do a full evaluation once an epoch is complete
            if epoch != old_epoch:
                self._session.run(self.epoch.assign_add(1))
                print("Epoch complete...old ", old_epoch)
                self.save_checkpoint()
                self.validate()
            old_epoch = epoch

        # Save model when done training
        self.save_checkpoint()

    def train_batch(self):
        """ Trains for one batch and returns cross entropy error """
        with tf.device("/cpu:0"):
            batch_input, batch_sub, batch_label = \
                self.data.next_train_batch()

        self._session.run(self.train_op,
                          {self.input: batch_input,
                           self.subreddit_input: batch_sub,
                           self.target: batch_label})

        return self._session.run(self.error,
                                 feed_dict={self.input: batch_input,
                                            self.subreddit_input: batch_sub,
                                            self.target: batch_label})
    def close_writers(self):
        """ Close tensorboard writers """
        self.train_writer.close()
        self.valid_writer.close()

