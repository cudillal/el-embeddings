#!/usr/bin/env python

import click as ck
import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow.python.framework import function
import re
import math
import matplotlib.pyplot as plt
import logging
from tensorflow.keras.layers import (
    Input,
)
from tensorflow.keras import optimizers
from tensorflow.keras.callbacks import ModelCheckpoint, EarlyStopping, CSVLogger
from tensorflow.keras import backend as K
from scipy.stats import rankdata


config = tf.ConfigProto(allow_soft_placement=True)
config.gpu_options.allow_growth = True
session = tf.Session(config=config)
K.set_session(session)

logging.basicConfig(level=logging.INFO)

@ck.command()
@ck.option(
    '--data-file', '-df', default='data/data-train/yeast-classes-normalized.owl',
    help='Normalized ontology file (Normalizer.groovy)')
@ck.option(
    '--valid-data-file', '-vdf', default='data/data-valid/4932.protein.actions.v10.5.txt',
    help='Validation data set')
@ck.option(
    '--out-classes-file', '-ocf', default='data/cls_embeddings.pkl',
    help='Pandas pkl file with class embeddings')
@ck.option(
    '--out-relations-file', '-orf', default='data/rel_embeddings.pkl',
    help='Pandas pkl file with relation embeddings')
@ck.option(
    '--batch-size', '-bs', default=256,
    help='Batch size')
@ck.option(
    '--epochs', '-e', default=1000,
    help='Training epochs')
@ck.option(
    '--device', '-d', default='gpu:0',
    help='GPU Device ID')
@ck.option(
    '--embedding-size', '-es', default=100,
    help='Embeddings size')
@ck.option(
    '--reg-norm', '-rn', default=1,
    help='Regularization norm')
@ck.option(
    '--margin', '-m', default=0.01,
    help='Loss margin')
@ck.option(
    '--learning-rate', '-lr', default=0.01,
    help='Learning rate')
@ck.option(
    '--params-array-index', '-pai', default=-1,
    help='Params array index')
@ck.option(
    '--loss-history-file', '-lhf', default='data/loss_history.csv',
    help='Pandas pkl file with loss history')
def main(data_file, valid_data_file, out_classes_file, out_relations_file,
         batch_size, epochs, device, embedding_size, reg_norm, margin,
         learning_rate, params_array_index, loss_history_file):
    # SLURM JOB ARRAY INDEX
    pai = params_array_index
    if params_array_index != -1:
        orgs = ['human', 'yeast']
        sizes = [50, 100, 200, 400]
        margins = [-0.1, -0.01, 0.0, 0.01, 0.1]
        reg_norms = [1,]
        reg_norm = reg_norms[0]
        margin = margins[params_array_index % 5]
        params_array_index //= 5
        embedding_size = sizes[params_array_index % 4]
        params_array_index //= 4
        org = orgs[params_array_index % 2]
        print('Params:', org, embedding_size, margin, reg_norm)
        
        data_file = f'data/data-train/{org}-classes-normalized.owl'
        if org == 'human':
            valid_data_file = f'data/data-valid/9606.protein.actions.v10.5.txt'
        out_classes_file = f'data/{org}_{pai}_{embedding_size}_{margin}_{reg_norm}_cls.pkl'
        out_relations_file = f'data/{org}_{pai}_{embedding_size}_{margin}_{reg_norm}_rel.pkl'
        loss_history_file = f'data/{org}_{pai}_{embedding_size}_{margin}_{reg_norm}_loss.csv'
    train_data, classes, relations = load_data(data_file)
    valid_data = load_valid_data(valid_data_file, classes, relations)
    print(train_data, classes, relations)
    
    proteins = {}
    for k, v in classes.items():
        if not k.startswith('<http://purl.obolibrary.org/obo/GO_'):
            proteins[k] = v
    
    nb_classes = len(classes)
    nb_relations = len(relations)
    nb_train_data = 0
    for key, val in train_data.items():
        nb_train_data = max(len(val), nb_train_data)
    train_steps = int(math.ceil(nb_train_data / (1.0 * batch_size)))
    train_generator = Generator(train_data, batch_size, steps=train_steps)

    cls_dict = {v: k for k, v in classes.items()}
    rel_dict = {v: k for k, v in relations.items()}

    cls_list = []
    rel_list = []
    for i in range(nb_classes):
        cls_list.append(cls_dict[i])
    for i in range(nb_relations):
        rel_list.append(rel_dict[i])

    with tf.device('/' + device):
        nf1 = Input(shape=(2,), dtype=np.int32)
        nf2 = Input(shape=(3,), dtype=np.int32)
        nf3 = Input(shape=(3,), dtype=np.int32)
        nf4 = Input(shape=(3,), dtype=np.int32)
        dis = Input(shape=(3,), dtype=np.int32)
        # neg = Input(shape=(2,), dtype=np.int32)
        el_model = ELModel(nb_classes, nb_relations, embedding_size, batch_size, margin, reg_norm)
        out = el_model([nf1, nf2, nf3, nf4, dis])
        model = tf.keras.Model(inputs=[nf1, nf2, nf3, nf4, dis], outputs=out)
        optimizer = optimizers.Adam(lr=0.003)
        model.compile(optimizer=optimizer, loss='mse')

        # TOP Embedding
        top = classes.get('owl:Thing', None)
        checkpointer = MyModelCheckpoint(
            out_classes_file=out_classes_file,
            out_relations_file=out_relations_file,
            cls_list=cls_list,
            rel_list=rel_list,
            valid_data=valid_data,
            proteins=proteins,
            monitor='loss',
            top=top)
        
        logger = CSVLogger(loss_history_file)

        # Save initial embeddings
        cls_embeddings = el_model.cls_embeddings.get_weights()[0]
        rel_embeddings = el_model.rel_embeddings.get_weights()[0]

        # Save embeddings of every thousand epochs
        # if (epoch + 1) % 1000 == 0:
        cls_file = f'{out_classes_file}_0.pkl'
        rel_file = f'{out_relations_file}_0.pkl'

        df = pd.DataFrame(
            {'classes': cls_list, 'embeddings': list(cls_embeddings)})
        df.to_pickle(cls_file)

        df = pd.DataFrame(
            {'relations': rel_list, 'embeddings': list(rel_embeddings)})
        df.to_pickle(rel_file)

        
        model.fit_generator(
            train_generator,
            steps_per_epoch=train_steps,
            epochs=epochs,
            workers=12,
            callbacks=[logger, checkpointer])


class ELModel(tf.keras.Model):

    def __init__(self, nb_classes, nb_relations, embedding_size, batch_size, margin=0.01, reg_norm=1):
        super(ELModel, self).__init__()
        self.nb_classes = nb_classes
        self.nb_relations = nb_relations
        self.margin = margin
        self.reg_norm = reg_norm
        self.batch_size = batch_size
        
        self.cls_embeddings = tf.keras.layers.Embedding(
            nb_classes,
            embedding_size + 1,
            input_length=1)
        self.rel_embeddings = tf.keras.layers.Embedding(
            nb_relations,
            embedding_size,
            input_length=1)

        top_embed = np.zeros((embedding_size + 1), dtype=np.float32)
        top_embed[-1] = 1000000.0 # Infinity radius
        self.top_embed = tf.convert_to_tensor(top_embed, dtype=tf.float32)
            
    def call(self, input):
        """Run the model."""
        nf1, nf2, nf3, nf4, dis = input
        loss1 = self.nf1_loss(nf1)
        loss2 = self.nf2_loss(nf2)
        loss3 = self.nf3_loss(nf3)
        loss4 = self.nf4_loss(nf4)
        loss_dis = self.dis_loss(dis)
        # loss_neg = self.neg_loss(neg)
        loss = loss1 + loss2 + loss3 + loss4 + loss_dis # + loss_neg
        return loss
   
    def loss(self, c, d):
        rc = tf.math.abs(c[:, -1])
        rd = tf.math.abs(d[:, -1])
        c = c[:, 0:-1]
        d = d[:, 0:-1]
        euc = tf.norm(c - d, axis=1)
        dst = tf.reshape(tf.nn.relu(euc + rc - rd), [-1, 1])
        return dst + self.reg(c) + self.reg(d)

    def reg(self, x):
        res = tf.abs(tf.norm(x, axis=1) - self.reg_norm)
        res = tf.reshape(res, [-1, 1])
        return res
        
    def nf1_loss(self, input):
        c = input[:, 0]
        d = input[:, 1]
        c = self.cls_embeddings(c)
        d = self.cls_embeddings(d)
        return self.loss(c, d)
    
    def nf2_loss(self, input):
        c = input[:, 0]
        d = input[:, 1]
        e = input[:, 2]
        c = self.cls_embeddings(c)
        d = self.cls_embeddings(d)
        e = self.cls_embeddings(e)
        rc = tf.reshape(tf.math.abs(c[:, -1]), [-1, 1])
        rd = tf.reshape(tf.math.abs(d[:, -1]), [-1, 1])
        re = tf.reshape(tf.math.abs(d[:, -1]), [-1, 1])
        sr = rc + rd
        x1 = c[:, 0:-1]
        x2 = d[:, 0:-1]
        x3 = e[:, 0:-1]
        x = x2 - x1
        dst = tf.reshape(tf.norm(x, axis=1), [-1, 1])
        dst2 = tf.reshape(tf.norm(x3 - x1, axis=1), [-1, 1])
        dst3 = tf.reshape(tf.norm(x3 - x2, axis=1), [-1, 1])
        rdst = tf.nn.relu(tf.math.minimum(rc, rd) - re)
        dst_loss = (tf.nn.relu(dst - sr)
                    + tf.nn.relu(dst2 - rc)
                    + tf.nn.relu(dst3 - rd)
                    + rdst - self.margin)
        return dst_loss + self.reg(x1) + self.reg(x2) + self.reg(x3)

    def nf3_loss(self, input):
        # C subClassOf R some D
        c = input[:, 0]
        r = input[:, 1]
        d = input[:, 2]
        c = self.cls_embeddings(c)
        d = self.cls_embeddings(d)
        r = self.rel_embeddings(r)
        rd = tf.concat([r, tf.zeros((self.batch_size, 1), dtype=tf.float32)], 1)
        c = c + rd
        return self.loss(c, d) # + self.reg(r)

    def nf4_loss(self, input):
        # R some C subClassOf D
        r = input[:, 0]
        c = input[:, 1]
        d = input[:, 2]
        c = self.cls_embeddings(c)
        d = self.cls_embeddings(d)
        r = self.rel_embeddings(r)
        rr = tf.concat([r, tf.zeros((self.batch_size, 1), dtype=tf.float32)], 1)
        c = c - rr
        # c - r should intersect with d
        rc = tf.reshape(tf.math.abs(c[:, -1]), [-1, 1])
        rd = tf.reshape(tf.math.abs(d[:, -1]), [-1, 1])
        sr = rc + rd
        x1 = c[:, 0:-1]
        x2 = d[:, 0:-1]
        x = x2 - x1
        dst = tf.reshape(tf.norm(x, axis=1), [-1, 1])
        dst_loss = tf.nn.relu(dst - sr - self.margin)
        return dst_loss + self.reg(x1) + self.reg(x2) # + self.reg(r)
    

    def dis_loss(self, input):
        c = input[:, 0]
        d = input[:, 1]
        c = self.cls_embeddings(c)
        d = self.cls_embeddings(d)
        rc = tf.reshape(tf.math.abs(c[:, -1]), [-1, 1])
        rd = tf.reshape(tf.math.abs(d[:, -1]), [-1, 1])
        sr = rc + rd
        x1 = c[:, 0:-1]
        x2 = d[:, 0:-1]
        x = x2 - x1
        dst = tf.reshape(tf.norm(x, axis=1), [-1, 1])
        return tf.nn.relu(sr - dst + self.margin) + self.reg(x1) + self.reg(x2)

    # def neg_loss(self, input, margin, reg_norm):
    #     c = input[:, 0]
    #     d = input[:, 1]
    #     c = self.cls_embeddings(c)
    #     d = self.cls_embeddings(d)
    #     rc = tf.reshape(tf.math.abs(c[:, -1]), [-1, 1])
    #     rd = tf.reshape(tf.math.abs(d[:, -1]), [-1, 1])
    #     x1 = c[:, 0:-1]
    #     x2 = d[:, 0:-1]
    #     x = x2 - x1
    #     dst = tf.reshape(norm(x), [-1, 1])
    #     return tf.nn.relu(rd - rc - dst + self.margin) + self.reg(x1) + self.reg(x2)
    

class MyModelCheckpoint(ModelCheckpoint):

    def __init__(self, *args, **kwargs):
        super(ModelCheckpoint, self).__init__()
        self.out_classes_file = kwargs.pop('out_classes_file')
        self.out_relations_file = kwargs.pop('out_relations_file')
        self.monitor = kwargs.pop('monitor')
        self.cls_list = kwargs.pop('cls_list')
        self.rel_list = kwargs.pop('rel_list')
        self.valid_data = kwargs.pop('valid_data')
        self.proteins = kwargs.pop('proteins')
        self.prot_index = list(self.proteins.values())
        self.prot_dict = {v: k for k, v in enumerate(self.prot_index)}
        self.top = kwargs.pop('top', None)
    
        self.best_rank = 100000

    def on_batch_begin(self, batch, logs=None):
        # Set TOP embedding
        if self.top:
            el_model = self.model.layers[-1]
            assign = tf.assign(
                el_model.cls_embeddings.embeddings[self.top, :], el_model.top_embed)
            session.run([assign])
        super(MyModelCheckpoint, self).on_batch_begin(batch, logs)
        
    def on_epoch_end(self, epoch, logs=None):
        # Save embeddings every 10 epochs
        current_loss = logs.get(self.monitor)
        if math.isnan(current_loss):
            print('NAN loss, stopping training')
            self.model.stop_training = True
            return
        if len(self.valid_data) == 0:
            return
        el_model = self.model.layers[-1]
        cls_embeddings = el_model.cls_embeddings.get_weights()[0]
        rel_embeddings = el_model.rel_embeddings.get_weights()[0]

        prot_embeds = cls_embeddings[self.prot_index]
        prot_rs = prot_embeds[:, -1].reshape(-1, 1)
        prot_embeds = prot_embeds[:, :-1]
        
        mean_rank = 0
        n = len(self.valid_data)
        
        for c, r, d in self.valid_data:
            c, r, d = self.prot_dict[c], r, self.prot_dict[d]
            ec = prot_embeds[c, :]
            rc = prot_rs[c, :]
            er = rel_embeddings[r, :]
            ec += er

            dst = np.linalg.norm(prot_embeds - ec.reshape(1, -1), axis=1)
            dst = dst.reshape(-1, 1)
            if rc > 0:
                overlap = np.maximum(0, (2 * rc - np.maximum(dst + rc - prot_rs - el_model.margin, 0)) / (2 * rc))
            else:
                overlap = (np.maximum(dst - prot_rs - el_model.margin, 0) == 0).astype('float32')
            
            edst = np.maximum(0, dst - rc - prot_rs - el_model.margin)
            res = (overlap + 1 / np.exp(edst)) / 2
            res = res.flatten()
            index = rankdata(-res, method='average')
            rank = index[d]
            mean_rank += rank
            # Filtered rank
            # index = rankdata(-(res * trlabels[r][c, :]), method='average')
            # rank = index[d]
            # fmean_rank += rank

        mean_rank /= n
        # fmean_rank /= n
        print(f'\n Validation {epoch + 1} {mean_rank}\n')
        if mean_rank < self.best_rank:
            self.best_rank = mean_rank
            print(f'\n Saving embeddings {epoch + 1} {mean_rank}\n')
            
            cls_file = self.out_classes_file
            rel_file = self.out_relations_file
            # Save embeddings of every thousand epochs
            # if (epoch + 1) % 1000 == 0:
            # cls_file = f'{cls_file}_{epoch + 1}.pkl'
            # rel_file = f'{rel_file}_{epoch + 1}.pkl'

            df = pd.DataFrame(
                {'classes': self.cls_list, 'embeddings': list(cls_embeddings)})
            df.to_pickle(cls_file)

            df = pd.DataFrame(
                {'relations': self.rel_list, 'embeddings': list(rel_embeddings)})
            df.to_pickle(rel_file)

        

class Generator(object):

    def __init__(self, data, batch_size=128, steps=100):
        self.data = data
        self.batch_size = batch_size
        self.steps = steps
        self.start = 0

    def __iter__(self):
        return self
    
    def __next__(self):
        return self.next()

    def reset(self):
        self.start = 0

    def next(self):
        if self.start < self.steps:
            nf1_index = np.random.choice(
                self.data['nf1'].shape[0], self.batch_size)
            nf2_index = np.random.choice(
                self.data['nf2'].shape[0], self.batch_size)
            nf3_index = np.random.choice(
                self.data['nf3'].shape[0], self.batch_size)
            nf4_index = np.random.choice(
                self.data['nf4'].shape[0], self.batch_size)
            dis_index = np.random.choice(
                self.data['disjoint'].shape[0], self.batch_size)
            # neg_index = np.random.choice(
            #     self.data['negatives'].shape[0], self.batch_size)
            nf1 = self.data['nf1'][nf1_index]
            nf2 = self.data['nf2'][nf2_index]
            nf3 = self.data['nf3'][nf3_index]
            nf4 = self.data['nf4'][nf4_index]
            dis = self.data['disjoint'][dis_index]
            # print(nf1, nf2, nf3, nf4, dis)
            # neg = self.data['negatives'][neg_index]
            labels = np.zeros((self.batch_size, 1), dtype=np.float32)
            self.start += 1
            return ([nf1, nf2, nf3, nf4, dis], labels)
        else:
            self.reset()


def load_data(filename, index=True):
    classes = {}
    relations = {}
    data = {'nf1': [], 'nf2': [], 'nf3': [], 'nf4': [], 'disjoint': []}
    with open(filename) as f:
        for line in f:
            # Ignore SubObjectPropertyOf
            if line.startswith('SubObjectPropertyOf'):
                continue
            # Ignore SubClassOf()
            line = line.strip()[11:-1]
            if not line:
                continue
            if line.startswith('ObjectIntersectionOf('):
                # C and D SubClassOf E
                it = line.split(' ')
                c = it[0][21:]
                d = it[1][:-1]
                e = it[2]
                if c not in classes:
                    classes[c] = len(classes)
                if d not in classes:
                    classes[d] = len(classes)
                if e not in classes:
                    classes[e] = len(classes)
                form = 'nf2'
                if e == 'owl:Nothing':
                    form = 'disjoint'
                if index:
                    data[form].append((classes[c], classes[d], classes[e]))
                else:
                    data[form].append((c, d, e))
                
            elif line.startswith('ObjectSomeValuesFrom('):
                # R some C SubClassOf D
                it = line.split(' ')
                r = it[0][21:]
                c = it[1][:-1]
                d = it[2]
                if c not in classes:
                    classes[c] = len(classes)
                if d not in classes:
                    classes[d] = len(classes)
                if r not in relations:
                    relations[r] = len(relations)
                if index:
                    data['nf4'].append((relations[r], classes[c], classes[d]))
                else:
                    data['nf4'].append((r, c, d))
            elif line.find('ObjectSomeValuesFrom') != -1:
                # C SubClassOf R some D
                it = line.split(' ')
                c = it[0]
                r = it[1][21:]
                d = it[2][:-1]
                if c not in classes:
                    classes[c] = len(classes)
                if d not in classes:
                    classes[d] = len(classes)
                if r not in relations:
                    relations[r] = len(relations)
                if index:
                    data['nf3'].append((classes[c], relations[r], classes[d]))
                else:
                    data['nf3'].append((c, r, d))
            else:
                # C SubClassOf D
                it = line.split(' ')
                c = it[0]
                d = it[1]
                if c not in classes:
                    classes[c] = len(classes)
                if d not in classes:
                    classes[d] = len(classes)
                if index:
                    data['nf1'].append((classes[c], classes[d]))
                else:
                    data['nf1'].append((c, d))
            
    data['nf1'] = np.array(data['nf1'])
    data['nf2'] = np.array(data['nf2'])
    data['nf3'] = np.array(data['nf3'])
    data['nf4'] = np.array(data['nf4'])
    data['disjoint'] = np.array(data['disjoint'])

    for key, val in data.items():
        index = np.arange(len(data[key]))
        np.random.seed(seed=100)
        np.random.shuffle(index)
        data[key] = val[index]
    
    return data, classes, relations

def load_valid_data(valid_data_file, classes, relations):
    data = []
    with open(valid_data_file, 'r') as f:
        for line in f:
            it = line.strip().split()
            id1 = f'<http://{it[0]}>'
            id2 = f'<http://{it[1]}>'
            rel = f'<http://{it[2]}>'
            if id1 not in classes or id2 not in classes or rel not in relations:
                continue
            data.append((classes[id1], relations[rel], classes[id2]))
    return data

if __name__ == '__main__':
    main()
