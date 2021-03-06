from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from torch.autograd import Variable
from tempfile import gettempdir
from six.moves import urllib
from six.moves import xrange  # pylint: disable=redefined-builtin
import torch
import argparse
import collections
import hashlib
import math
import os
import random
import sys
import zipfile
import pickle
import numpy as np
import tensorflow as tf
import torch.nn.functional as F
import torch.utils.data as data_utils

data_index = 0

def tokenize_phrase(data): #utilizado para pegar as frases (anuncio) e separar elas em tokens
    tokens = [s[1][0].split() for s in data.iterrows()]
    return tokens

def labels(data): #utiliza um dicionario para criar o label dos dados a serem treinados
    dic = dict()
    ret = []
    i=0
    for s in data.iterrows():
        if s[1][3] in dic:
            ret.append(dic[s[1][3]])
        else:
            dic[s[1][3]]=i
            ret.append(i)
            i+=1
    return dic, ret

def create_test(data,labels): #cria os labels para o test dataset
    i=0
    tks_tst = []
    label_tst = []
    while i<len(data):
        tks_tst.append(data[i])
        label_tst.append(labels[i])
        i+=1000
    return tks_tst, label_tst

def convert_int(tokens, dic): #faz a conversao dos tokens para int, para serem utilizados nos treinamentos junto com os embedding pre treinados
    tmp = np.zeros((len(tokens),24),dtype=int)
    j=0
    k=0
    for i in tokens:
        k=0
        for s in i:
            if (s in dic):
                tmp[j][k] = dic[s]
            k+=1
        j+=1
    return tmp

cuda0 = torch.device('cuda:0') #caso esteja treinando com gpu


port = pickle.load( open( "port.p", "rb" ) ) #carrega o dataframe limpo em portugues
tks = tokenize_phrase(port) #faz a tokenizacao das frases
unused_dic = pickle.load( open( "unused_dic.p", "rb" ) )  #carrega o dicionario treinado anteriormente com os embeddings
tks=convert_int(tks,unused_dic) #converte os tokens palavras para int
labels2 = port.category.unique() #importa os labels
#IMPORTAR OS EMBEDDINGS UTILIZANDO PICKLE
embeds_final = pickle.load( open( "embeds_final_np.p", "rb" ) )
final_embeds=torch.from_numpy(embeds_final).float().to(cuda0)
dic_label, labels_num = labels(port) #dic label, transforma os labels em numero
labels_num = np.array(labels_num) #transforma de lista pra numpy
#labels_num.shape


def train(attention_model,train_loader,criterion,optimizer,epochs = 5,use_regularization = False,C=0,clip=False):
    """
        Training code
        Args:
            attention_model : modelo de atenção para ser treinado
            train_loader    : dataloader para enviar os dados para o treino
            optimizer       :  optimizer
            criterion       :  Função de Loss
            epochs          : {int} numero de peochs
            use_regularizer : {bool} regularizador para evitar que attentions vejam a mesma coisa
            C               : {int} coeficiente de penalizaçao
            clip            : {bool} gradiente clipping, utilizado para o treino
       
        Returns:
            accuracy e perda do modelo
        """
    losses = []
    accuracy = []
    for i in range(epochs):
        print("Running EPOCH",i+1)
        total_loss = 0
        n_batches = 0
        correct = 0  
        for batch_idx,train in enumerate(train_loader):
            attention_model.hidden_state = attention_model.init_hidden()
            x,y = Variable(train[0]).to('cuda:0'),Variable(train[1]).to('cuda:0')
            y_pred,att = attention_model(x)
            #penalizando para evitar que as atencoes foquem na mesma coisa AAT - I
            if use_regularization:
                attT = att.transpose(1,2)
                identity = torch.eye(att.size(1))
                identity = Variable(identity.unsqueeze(0).expand(train_loader.batch_size,att.size(1),att.size(1))).to('cuda:0')
                penal = attention_model.l2_matrix_norm(att@attT - identity)  
            if not bool(attention_model.type) :
                #classificacao binaria! bom para o caso da deteccao de exames
                #Adiciona um valor pequeno para evitar que a resposta seja zero
                correct+=torch.eq(torch.round(y_pred.type(torch.DoubleTensor).squeeze(1)),y).data.sum()
                if use_regularization:
                    try:
                        loss = criterion(y_pred.type(torch.DoubleTensor).squeeze(1)+1e-8,y) + C * penal/train_loader.batch_size         
                    except RuntimeError:
                        raise Exception("BCELoss gets nan values on regularization. Either remove regularization or add very small values")
                else:
                    loss = criterion(y_pred.type(torch.DoubleTensor).squeeze(1),y)
            else:  
                correct+=torch.eq(torch.max(y_pred,1)[1],y.type(torch.cuda.LongTensor)).data.sum()
                if use_regularization:
                    loss = criterion(y_pred,y) + (C * penal/train_loader.batch_size).type(torch.cuda.FloatTensor)
                else:
                    loss = criterion(y_pred,y)
            total_loss+=loss.data
            optimizer.zero_grad()
            loss.backward()
            #gradient clipping
            if clip:
                torch.nn.utils.clip_grad_norm(attention_model.parameters(),0.5)
            optimizer.step()
            n_batches+=1
            #mais coisa legada que eu usei para ver se o modelo estava funcionando
            #print("correct value is ",correct)
            #print("avg ateh la eh ",correct.float()/(n_batches*train_loader.batch_size) )
            #print("numero de tries",(n_batches*train_loader.batch_size) )
        print("avg_loss is",total_loss/n_batches)
        print("Accuracy of the model",correct.float()/(n_batches*train_loader.batch_size))
        w_ii2, w_if2, w_ic2, w_io2 = attention_model.lstm.weight_ih_l0.chunk(4, 0) 
        print(w_ii2)
        losses.append(total_loss/n_batches)
        accuracy.append(correct.float()/(n_batches*train_loader.batch_size))
    return losses,accuracy
 
 
def evaluate(attention_model,x_test,y_test):
    """
        cv results
        Args:
            attention_model : {object} model
            x_test          : {nplist} x_test
            y_test          : {nplist} y_test
        Returns:
            cv-accuracy
    """
    attention_model.batch_size = x_test.shape[0]
    attention_model.hidden_state = attention_model.init_hidden()
    x_test_var = Variable(torch.from_numpy(x_test).type(torch.LongTensor))
    y_test_pred,_ = attention_model(x_test_var)
    if bool(attention_model.type):
        y_preds = torch.max(y_test_pred,1)[1]
        y_test_var = Variable(torch.from_numpy(y_test).type(torch.LongTensor))
    else:
        y_preds = torch.round(y_test_pred.type(torch.DoubleTensor).squeeze(1))
        y_test_var = Variable(torch.from_numpy(y_test).type(torch.DoubleTensor))
    return torch.eq(y_preds,y_test_var).data.sum()/x_test_var.size(0)



#O modelo de atencao em si
class StructuredSelfAttention(torch.nn.Module):
    """
    A implementaçao em si da classe do artigo.
    """
    def __init__(self,batch_size,lstm_hid_dim,d_a,r,max_len,emb_dim=128,vocab_size=None,use_pretrained_embeddings = True,embeddings=None,type=0,
                int_dim1 = 256, int_dim2 = 2048, n_classes = 1):
        """
        Initializar os parametros sugeridos no artigo
        Args:
            batch_size  : {int} batch_size utilizado para treino
            lstm_hid_dim: {int} dimensao das lstm
            d_a         : {int} hiperparametro: dimensoes da representacao interna da atencao
            r           : {int} attention heads
            max_len     : {int} maximo comprimento de frases
            emb_dim     : {int} dimensao do embedding
            vocab_size  : {int} tamanho do vocabulario
            use_pretrained_embeddings: {bool} usar o proprio embedding criado aqui ou algum pré-treinado #legado! nao uso mais
            embeddings  : {torch.FloatTensor} os embeddings pre-treinado 
            type        : [0,1] 0-->binary_classification 1-->multiclass classification
            n_classes   : {int} numero de classes
        Returns:
            self
 
        Raises:
            Exception
        """
        super(StructuredSelfAttention,self).__init__()    
        self.embeddings = torch.nn.Embedding(embeddings.size(0), embeddings.size(1))
        self.embeddings.weight = torch.nn.Parameter(embeddings)
        self.emb_dim = embeddings.size(1)
        self.lstm = torch.nn.LSTM(emb_dim,lstm_hid_dim,1,batch_first=True)
        self.ws1 = torch.nn.Linear(lstm_hid_dim,d_a) #WS1
        self.ws1.bias.data.fill_(0) #bias zerado
        self.ws2 = torch.nn.Linear(d_a,r) #Ws2, para o caso de multipla atenção
        self.ws2.bias.data.fill_(0) 
        self.n_classes = n_classes #numero de classes para ser utilizada na classificaçaão
        self.inter1 = torch.nn.Linear(lstm_hid_dim,int_dim1) #legado tb! uso direto da lstm para a classe intermediaria
        self.inter2 = torch.nn.Linear(lstm_hid_dim,2048)
        self.linear_final = torch.nn.Linear(2048,self.n_classes) #posso mudar esse parametro
        self.batch_size = batch_size       
        self.max_len = max_len #tamanho maximo de uma frase
        self.lstm_hid_dim = lstm_hid_dim
        self.hidden_state = self.init_hidden()
        self.r = r
        self.type = type     
    def softmax(self,input, axis=1): #aplica a normalizacao nos pesos da atencao
        """
        Softmax aplicado ao eixo = n
        Args:
           input: {Tensor,Variable} input a qual a normalizacao sera aplicada
           axis : {int} axis em que a normalizacao sera aplicada
        Returns:
            tensor normalizado
        """
        input_size = input.size()
        trans_input = input.transpose(axis, len(input_size)-1)
        trans_size = trans_input.size()
        input_2d = trans_input.contiguous().view(-1, trans_size[-1])
        soft_max_2d = F.softmax(input_2d)
        soft_max_nd = soft_max_2d.view(*trans_size)
        return soft_max_nd.transpose(axis, len(input_size)-1)
    def init_hidden(self):
        return (Variable(torch.zeros(1,self.batch_size,self.lstm_hid_dim)).to('cuda:0'),Variable(torch.zeros(1,self.batch_size,self.lstm_hid_dim)).to('cuda:0'))
    def forward(self,x):
        embeddings = self.embeddings(x)       
        outputs, self.hidden_state = self.lstm(embeddings.view(self.batch_size,self.max_len,-1),self.hidden_state)
        x = F.tanh(self.ws1(outputs)) #preparando para multiplicar pela segunda matriz       
        x = self.ws2(x)       
        x = self.softmax(x,1)       #matriz dos coeficientes de atencao
        attention = x.transpose(1,2)       
        sentence_embeddings = attention@outputs       
        avg_sentence_embeddings = torch.sum(sentence_embeddings,1)/self.r #soma os embeddings de cada attention head
        #print(avg_sentence_embeddings.shape)
        #inter_1 = F.sigmoid(self.inter1(avg_sentence_embeddings))
        #print(inter_1.shape)
        inter_2 = F.sigmoid(self.inter2(avg_sentence_embeddings))
        #print(inter_2.shape)
        return F.log_softmax(self.linear_final(inter_2)),attention
	#Regularization
    def l2_matrix_norm(self,m):
        """
        Frobenius norm calculation
        Args:
           m: {Variable} ||AAT - I||
        Returns:
            regularized value
        """
        return torch.sum(torch.sum(torch.sum(m**2,1),1)**0.5).type(torch.DoubleTensor) #regularizacao para evitar que as attention heads fiquem parecidas

def multiclass_classification(attention_model,train_loader,epochs=5,use_regularization=True,C=1.0,clip=True):
    loss = torch.nn.NLLLoss()
    optimizer = torch.optim.RMSprop(attention_model.parameters())
    train(attention_model,train_loader,loss,optimizer,epochs,use_regularization,C,clip)

final_embeds=torch.from_numpy(embeds_final).float().to(cuda0) #embeddings passando de numpy para tensor no cuda


#loader para treinar os dados
train_data = data_utils.TensorDataset(torch.from_numpy(tks).type(torch.LongTensor).to(cuda0),torch.from_numpy(labels_num).type(torch.LongTensor).to(cuda0))

batch_size = 512
train_loader = data_utils.DataLoader(train_data,batch_size=batch_size,drop_last=True)

attention_model = StructuredSelfAttention(batch_size=train_loader.batch_size,
    lstm_hid_dim=50,d_a = 100,r=10,
    vocab_size=50000,max_len=24,type=1,n_classes=1576,use_pretrained_embeddings=True,
    embeddings=final_embeds).to(cuda0)

multiclass_classification(attention_model,train_loader,epochs=10,use_regularization=True,C=0.03,clip=True)