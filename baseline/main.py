import pandas as pd
import numpy as np
import argparse
import os
import nsml
from nsml import HAS_DATASET, DATASET_PATH
from glob import glob
import json
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras.preprocessing.text import Tokenizer 
from tensorflow.keras.preprocessing.sequence import pad_sequences
from tensorflow.keras.layers import Input, LSTM, Embedding, Dense, Concatenate
from tensorflow.keras.models import Model
from tensorflow.keras.callbacks import EarlyStopping, ModelCheckpoint
import pickle
import warnings
warnings.filterwarnings(action='ignore')


from tensorflow.keras.layers import Layer
from tensorflow.keras import backend as K

from tensorflow.python.framework.ops import disable_eager_execution
disable_eager_execution()

print(f'tensorflow version: {tf.__version__}')

class AttentionLayer(Layer):
    """
    This class implements Bahdanau attention (https://arxiv.org/pdf/1409.0473.pdf).
    There are three sets of weights introduced W_a, U_a, and V_a
     """

    def __init__(self, **kwargs):
        super(AttentionLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        assert isinstance(input_shape, list)
        # Create a trainable weight variable for this layer

        self.W_a = self.add_weight(name='W_a',
                                   shape=tf.TensorShape((input_shape[0][2], input_shape[0][2])),
                                   initializer='uniform',
                                   trainable=True)
        self.U_a = self.add_weight(name='U_a',
                                   shape=tf.TensorShape((input_shape[1][2], input_shape[0][2])),
                                   initializer='uniform',
                                   trainable=True)
        self.V_a = self.add_weight(name='V_a',
                                   shape=tf.TensorShape((input_shape[0][2], 1)),
                                   initializer='uniform',
                                   trainable=True)

        super(AttentionLayer, self).build(input_shape)  # Be sure to call this at the end

    def call(self, inputs, verbose=False):
        """
        inputs: [encoder_output_sequence, decoder_output_sequence]
        """
        assert type(inputs) == list
        encoder_out_seq, decoder_out_seq = inputs
        if verbose:
            print('encoder_out_seq>', encoder_out_seq.shape)
            print('decoder_out_seq>', decoder_out_seq.shape)

        def energy_step(inputs, states):
            """ Step function for computing energy for a single decoder state
            inputs: (batchsize * 1 * de_in_dim)
            states: (batchsize * 1 * de_latent_dim)
            """

            assert_msg = "States must be an iterable. Got {} of type {}".format(states, type(states))
            assert isinstance(states, list) or isinstance(states, tuple), assert_msg

            """ Some parameters required for shaping tensors"""
            en_seq_len, en_hidden = encoder_out_seq.shape[1], encoder_out_seq.shape[2]
            de_hidden = inputs.shape[-1]

            """ Computing S.Wa where S=[s0, s1, ..., si]"""
            # <= batch size * en_seq_len * latent_dim
            W_a_dot_s = K.dot(encoder_out_seq, self.W_a)

            """ Computing hj.Ua """
            U_a_dot_h = K.expand_dims(K.dot(inputs, self.U_a), 1)  # <= batch_size, 1, latent_dim
            if verbose:
                print('Ua.h>', U_a_dot_h.shape)

            """ tanh(S.Wa + hj.Ua) """
            # <= batch_size*en_seq_len, latent_dim
            Ws_plus_Uh = K.tanh(W_a_dot_s + U_a_dot_h)
            if verbose:
                print('Ws+Uh>', Ws_plus_Uh.shape)

            """ softmax(va.tanh(S.Wa + hj.Ua)) """
            # <= batch_size, en_seq_len
            e_i = K.squeeze(K.dot(Ws_plus_Uh, self.V_a), axis=-1)
            # <= batch_size, en_seq_len
            e_i = K.softmax(e_i)

            if verbose:
                print('ei>', e_i.shape)

            return e_i, [e_i]

        def context_step(inputs, states):
            """ Step function for computing ci using ei """

            assert_msg = "States must be an iterable. Got {} of type {}".format(states, type(states))
            assert isinstance(states, list) or isinstance(states, tuple), assert_msg

            # <= batch_size, hidden_size
            c_i = K.sum(encoder_out_seq * K.expand_dims(inputs, -1), axis=1)
            if verbose:
                print('ci>', c_i.shape)
            return c_i, [c_i]

        fake_state_c = K.sum(encoder_out_seq, axis=1)
        fake_state_e = K.sum(encoder_out_seq, axis=2)  # <= (batch_size, enc_seq_len, latent_dim

        """ Computing energy outputs """
        # e_outputs => (batch_size, de_seq_len, en_seq_len)
        last_out, e_outputs, _ = K.rnn(
            energy_step, decoder_out_seq, [fake_state_e],
        )

        """ Computing context vectors """
        last_out, c_outputs, _ = K.rnn(
            context_step, e_outputs, [fake_state_c],
        )

        return c_outputs, e_outputs

    def compute_output_shape(self, input_shape):
        """ Outputs produced by the layer """
        return [
            tf.TensorShape((input_shape[1][0], input_shape[1][1], input_shape[1][2])),
            tf.TensorShape((input_shape[1][0], input_shape[1][1], input_shape[0][1]))
        ]

def make_model(text_max_len, hidden_size, src_vocab, embedding_dim, tar_vocab):
    encoder_inputs = Input(shape=(text_max_len,))
    enc_emb = Embedding(src_vocab, embedding_dim)(encoder_inputs)
    encoder_lstm1 = LSTM(hidden_size, return_sequences=True, return_state=True, dropout=0.4, recurrent_dropout=0.4)
    encoder_output1, state_h1, state_c1 = encoder_lstm1(enc_emb)
    encoder_lstm2 = LSTM(hidden_size, return_sequences=True, return_state=True, dropout=0.4, recurrent_dropout=0.4)
    encoder_output2, state_h2, state_c2 = encoder_lstm2(encoder_output1)
    encoder_lstm3 = LSTM(hidden_size, return_state=True, return_sequences=True, dropout=0.4, recurrent_dropout=0.4)
    encoder_outputs, state_h, state_c = encoder_lstm3(encoder_output2)

    decoder_inputs = Input(shape=(None,))
    dec_emb_layer = Embedding(tar_vocab, embedding_dim)
    dec_emb = dec_emb_layer(decoder_inputs)
    decoder_lstm = LSTM(hidden_size, return_sequences=True, return_state=True, dropout=0.4, recurrent_dropout=0.2)
    decoder_outputs, _, _ = decoder_lstm(dec_emb, initial_state=[state_h, state_c])

    attn_layer = AttentionLayer(name='attention_layer')
    attn_out, attn_states = attn_layer([encoder_outputs, decoder_outputs])

    decoder_concat_input = Concatenate(axis = -1, name='concat_layer')([decoder_outputs, attn_out])

    decoder_softmax_layer = Dense(tar_vocab, activation='softmax')
    decoder_softmax_outputs = decoder_softmax_layer(decoder_concat_input)
    model = Model([encoder_inputs, decoder_inputs], decoder_softmax_outputs)

    return model


class Preprocess:

    @staticmethod
    def make_dataset_list(path_list):
        json_data_list = []

        for path in path_list:
            with open(path) as f:
                json_data_list.append(json.load(f))

        return json_data_list

    @staticmethod
    def make_set_as_df(train_set, is_train = True):

        if is_train:
            train_dialogue = []
            train_dialogue_id = []
            train_summary = []
            for topic in train_set:
                for data in topic['data']:
                    train_dialogue_id.append(data['header']['dialogueInfo']['dialogueID'])
                    train_dialogue.append(''.join([dialogue['utterance'] for dialogue in data['body']['dialogue']]))
                    train_summary.append(data['body']['summary'])

            train_data = pd.DataFrame(
                {
                    'dialogueID': train_dialogue_id,
                    'dialogue': train_dialogue,
                    'summary': train_summary
                }
            )
            return train_data

        else:
            test_dialogue = []
            test_dialogue_id = []
            for topic in test_set:
                for data in topic['data']:
                    test_dialogue_id.append(data['header']['dialogueInfo']['dialogueID'])
                    test_dialogue.append(''.join([dialogue['utterance'] for dialogue in data['body']['dialogue']]))

            test_data = pd.DataFrame(
                {
                    'dialogueID': test_dialogue_id,
                    'dialogue': test_dialogue
                }
            )
            return test_data

    @staticmethod
    def train_valid_split(train_set, split_point):
        train_data = train_set.iloc[:split_point, :]
        val_data = train_set.iloc[split_point:, :]

        return train_data, val_data

    @staticmethod
    def make_model_input(dataset, is_valid=False, is_test = False):
        if is_test:
            encoder_input = dataset['dialogue']
            decoder_input = ['sostoken'] * len(dataset)
            return encoder_input, decoder_input

        elif is_valid:
            encoder_input = dataset['dialogue']
            decoder_input = ['sostoken'] * len(dataset)
            decoder_output = dataset['summary'].apply(lambda x: str(x) + 'eostoken')

            return encoder_input, decoder_input, decoder_output

        else:
            encoder_input = dataset['dialogue']
            decoder_input = dataset['summary'].apply(lambda x : 'sostoken' + str(x))
            decoder_output = dataset['summary'].apply(lambda x : str(x) + 'eostoken')

            return encoder_input, decoder_input, decoder_output


def seq2text(input_seq):
    temp=''
    for i in input_seq:
        if(i!=0):
            temp = temp + src_index_to_word[i]+' '
    return temp

def seq2summary(input_seq):
    temp=''
    for i in input_seq:
        if((i!=0 and i!=tar_word_to_index['sostoken']) and i!=tar_word_to_index['eostoken']):
            temp = temp + tar_index_to_word[i] + ' '
    return temp

def train_data_loader(root_path) :
    train_path = os.path.join(root_path, 'train', 'train_data', '*')
    pathes = glob(train_path)
    return pathes

def train(model) :

    es = EarlyStopping(monitor='val_loss', mode='min', verbose=1, patience = 2)
    print('training start!')
    model.fit(x = [encoder_input_train, decoder_input_train],
              y = decoder_target_train,
              validation_data = ([encoder_input_val, decoder_input_val], decoder_target_val),
              batch_size = 100, callbacks=[es], epochs = 1)

def decode_idx2word(sequence, tar_index_to_word):
    sentence = [tar_index_to_word[idx] for idx in sequence]
    sentence = ' '.join(sentence)
    return sentence


def bind_model(model, parser):
    # 학습한 모델을 저장하는 함수입니다.
    def save(dir_name, *parser):
        # directory
        os.makedirs(dir_name, exist_ok=True)
        save_dir = os.path.join(dir_name, 'model')
        model.save_weights(save_dir)
        
        with open(os.path.join(dir_name, "dict_for_infer"), "wb") as f:
            pickle.dump(dict_for_infer, f)

        print("저장 완료!")

    # 저장한 모델을 불러올 수 있는 함수입니다.
    def load(dir_name, *parser):
        save_dir = os.path.join(dir_name, 'model')
        model.load_weights(save_dir)

        global dict_for_infer
        with open(os.path.join(dir_name, "dict_for_infer"), 'rb') as f:
            dict_for_infer = pickle.load(f)
        
        print("로딩 완료!")

    def infer(test_path, **kwparser):

        tar_index_to_word = dict_for_infer['tar_index_to_word']
        src_tokenizer = dict_for_infer['src_tokenizer']
        tar_tokenizer = dict_for_infer['tar_tokenizer']

        print('tar_index_to_word: \n', tar_index_to_word)

        preprocessor = Preprocess()

        test_json_path = os.path.join(test_path, 'test_data', '*')
        print(f'test_json_path :\n{test_json_path}')
        test_path_list = glob(test_json_path)
        test_path_list.sort()
        print(f'test_path_list :\n{test_path_list}')

        test_json_list = preprocessor.make_dataset_list(test_path_list)
        test_data = preprocessor.make_set_as_df(test_json_list)
  
        print(f'test_data:\n{test_data["dialogue"]}')
        encoder_input_test, decoder_input_test = preprocessor.make_model_input(test_data, is_test= True)

        text_max_len = 100

        encoder_input_test = src_tokenizer.texts_to_sequences(encoder_input_test)
        decoder_input_test = tar_tokenizer.texts_to_sequences(decoder_input_test)
        encoder_input_test = pad_sequences(encoder_input_test, maxlen = text_max_len, padding='post')
        decoder_input_test = pad_sequences(decoder_input_test, maxlen = text_max_len, padding='post')
        total_data = len(encoder_input_test)
        batch = 100
        
        for i in range(0, total_data, batch):
            if i == 0:
                summary = model.predict([encoder_input_test[i:i+batch], decoder_input_test[i:i + batch]]).argmax(2)
                summary = [decode_idx2word(batch_idx, tar_index_to_word) for batch_idx in summary]
            else:
                predict = model.predict([encoder_input_test[i:i+batch], decoder_input_test[i:i + batch]]).argmax(2)
                predict = [decode_idx2word(batch_idx, tar_index_to_word) for batch_idx in predict]
                summary.extend(predict)
                
        
        test_id = test_data['dialogueID']
        
        # DONOTCHANGE: They are reserved for nsml
        # 리턴 결과는 [(id, summary)] 의 형태로 보내야만 리더보드에 올릴 수 있습니다.
        # ex)[(' efe21026-0715-5ca4-99fe-46d0ecfba147', '철수는 밥을 먹었다.'), ...]

        return list(zip(test_id, summary))

    # DONOTCHANGE: They are reserved for nsml
    # nsml에서 지정한 함수에 접근할 수 있도록 하는 함수입니다.
    nsml.bind(save=save, load=load, infer=infer)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='nia_test')
    parser.add_argument('--mode', type=str, default='train')
    parser.add_argument('--epochs', type=int, default=1)
    parser.add_argument('--iteration', type=str, default='0')
    parser.add_argument('--pause', type=int, default=0)
    args = parser.parse_args()

    embedding_dim = 128
    hidden_size = 256
    tar_vocab = 150000
    text_max_len = 100
    src_vocab = 150000
    summary_max_len = 100

    model = make_model(text_max_len, hidden_size, src_vocab, embedding_dim, tar_vocab)
    model.compile(optimizer='adam', loss='sparse_categorical_crossentropy')


    bind_model(model=model, parser=args)
    if args.pause :
        nsml.paused(scope=locals())

    if args.mode == 'train' :
        train_path_list = train_data_loader(DATASET_PATH)
        train_path_list.sort()

        preprocessor = Preprocess()


        train_json_list = preprocessor.make_dataset_list(train_path_list)

        train_data= preprocessor.make_set_as_df(train_json_list)

        split_point = int(len(train_data) * 0.9)

        train_set, valid_set = preprocessor.train_valid_split(train_data, split_point)

        encoder_input_train, decoder_input_train, decoder_output_train = preprocessor.make_model_input(train_set)
        encoder_input_val, decoder_input_val, decoder_output_val = preprocessor.make_model_input(valid_set, is_valid = True)

        src_tokenizer = Tokenizer(num_words=src_vocab)
        src_tokenizer.fit_on_texts(encoder_input_train)

        encoder_input_train = src_tokenizer.texts_to_sequences(encoder_input_train)
        encoder_input_val = src_tokenizer.texts_to_sequences(encoder_input_val)

        tar_tokenizer = Tokenizer(num_words = tar_vocab) 
        tar_tokenizer.fit_on_texts(decoder_input_train)


        decoder_input_train = tar_tokenizer.texts_to_sequences(decoder_input_train)
        decoder_target_train = tar_tokenizer.texts_to_sequences(decoder_output_train)
        decoder_input_val = tar_tokenizer.texts_to_sequences(decoder_input_val)
        decoder_target_val = tar_tokenizer.texts_to_sequences(decoder_output_val)

        encoder_input_train = pad_sequences(encoder_input_train, maxlen = text_max_len, padding='post')
        encoder_input_val = pad_sequences(encoder_input_val, maxlen = text_max_len, padding='post')
        decoder_input_train = pad_sequences(decoder_input_train, maxlen = summary_max_len, padding='post')
        decoder_target_train = pad_sequences(decoder_target_train, maxlen = summary_max_len, padding='post')
        decoder_input_val = pad_sequences(decoder_input_val, maxlen = summary_max_len, padding='post')
        decoder_target_val = pad_sequences(decoder_target_val, maxlen = summary_max_len, padding='post')

    
        src_index_to_word = src_tokenizer.index_word
        tar_word_to_index = tar_tokenizer.word_index
        tar_index_to_word = tar_tokenizer.index_word
        tar_index_to_word[0] = 'unk'

        dict_for_infer = {
            'tar_tokenizer' : tar_tokenizer,
            'src_tokenizer' : src_tokenizer,
            'embedding_dim' : embedding_dim,
            'src_vocab' : src_vocab,
            'tar_vocab' : tar_vocab,
            'hidden_size' : hidden_size,
            'text_max_len' : text_max_len,
            'summary_max_len' : summary_max_len,
            'tar_index_to_word' : tar_index_to_word
        }

        for epoch in range(args.epochs):
            print('학습 시작!')
            train(model)

            # DONOTCHANGE (You can decide how often you want to save the model)
            nsml.save(epoch)
