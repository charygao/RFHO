"""
This module contains utility functions to process and load various datasets. Most of the datasets are public,
but are not included in the package; MNIST dataset will be automatically downloaded.

There are also some classes to represent datasets. `ExampleVisiting` is an helper class that implements
the stochastic sampling of data and is optimized to work with `Reverse/ForwardHyperGradient` (has helper funcitons
to create training and validation `feed_dict` suppliers).
"""
from collections import OrderedDict

import numpy as np
from functools import reduce
import tensorflow as tf
from tensorflow.examples.tutorials.mnist.input_data import read_data_sets
import os
from rfho.utils import as_list, np_normalize_data, merge_dicts

import sys

try:
    import pandas as pd
except ImportError:
    pd = None
    print(sys.exc_info())
    print('pandas not found. Some load function might not work')
try:
    import scipy.io as scio
    from scipy import linalg
    import scipy.sparse as sc_sp
    import scipy as sp

    SPARSE_SCIPY_MATRICES = (sc_sp.csr.csr_matrix, sc_sp.coo.coo_matrix)
except ImportError:
    scio, linalg, sp, sc_sp = None, None, None, None
    SPARSE_SCIPY_MATRICES = ()
    print(sys.exc_info())
    print('scipy not found. Some load function might not work')

try:
    import sklearn.datasets as sk_dt
    from sklearn.utils import shuffle as sk_shuffle
except ImportError:
    sk_dt, sk_shuffle = None, None
    print('sklearn not found. Some load function might not work')

try:
    import intervaltree as it
except ImportError:
    it = None
    print(sys.exc_info())
    print('intervaltree not found. WindowedData will not work. (You can get intervaltree with pip!)')

import _pickle as cpickle

from_env = os.getenv('RFHO_DATA_FOLDER')
if from_env:
    DATA_FOLDER = from_env
    # print('Congratulations, RFHO_DATA_FOLDER found!')
else:
    print('Environment variable RFHO_DATA_FOLDER not found. Variables HELP_WIN and HELP_UBUNTU contain info.')
    DATA_FOLDER = os.getcwd()
    _COMMON_BEGIN = "You can set environment variable RFHO_DATA_FOLDER to" \
                    "specify root folder in which you store various datasets. \n"
    _COMMON_END = """\n
    You can also skip this step... \n
    In this case all load_* methods take a FOLDER path as first argument. \n
    Bye."""
    HELP_UBUNTU = _COMMON_BEGIN + """
    Bash command is: export RFHO_DATA_FOLDER='absolute/path/to/dataset/folder \n
    Remember! To add the global variable kinda permanently in your system you should add export command in
          bash.bashrc file located in etc folder.
    """ + _COMMON_END

    HELP_WIN = _COMMON_BEGIN + """
    Cmd command is: Set RFHO_DATA_FOLDER absolute/path/to/dataset/folder  for one session. \n
    To set it permanently use SetX instead of Set (and probably reboot system)
    """ + _COMMON_END

print('Data folder is', DATA_FOLDER)

# kind of private
TIMIT_DIR = os.path.join(DATA_FOLDER, 'timit4python')
XRMB_DIR = os.path.join(DATA_FOLDER, 'XRMB')
IROS15_BASE_FOLDER = os.path.join(DATA_FOLDER, os.path.join('dls_collaboration', 'Learning'))

# easy to find!
IRIS_TRAINING = os.path.join(DATA_FOLDER, 'iris', "training.csv")
IRIS_TEST = os.path.join(DATA_FOLDER, 'iris', "test.csv")
MNIST_DIR = os.path.join(DATA_FOLDER, "mnist_data")
CALTECH101_30_DIR = os.path.join(DATA_FOLDER, "caltech101-30")
CALTECH101_DIR = os.path.join(DATA_FOLDER, "caltech")
CENSUS_TRAIN = os.path.join(DATA_FOLDER, 'census', "train.csv")
CENSUS_TEST = os.path.join(DATA_FOLDER, 'census', "test.csv")
CIFAR10_DIR = os.path.join(DATA_FOLDER, "CIFAR-10")
CIFAR100_DIR = os.path.join(DATA_FOLDER, "CIFAR-100")
REALSIM = os.path.join(DATA_FOLDER, "realsim")

# scikit learn datasets
SCIKIT_LEARN_DATA = os.path.join(DATA_FOLDER, 'scikit_learn_data')


class Datasets:
    """
    Simple object for standard datasets. Has the field `train` `validation` and `test` and support indexing
    """

    def __init__(self, train=None, validation=None, test=None):
        self.train = train
        self.validation = validation
        self.test = test
        self._lst = [train, validation, test]

    def setting(self):
        return {k: v.setting() if hasattr(v, 'setting') else None for k, v in vars(self).items()}

    def __getitem__(self, item):
        return self._lst[item]

    def __len__(self):
        return len([_ for _ in self._lst if _ is not None])

    @staticmethod
    def from_list(list_of_datasets):
        """
        Generates a `Datasets` object from a list.

        :param list_of_datasets: list containing from one to three dataset
        :return:
        """
        train, valid, test = None, None, None
        train = list_of_datasets[0]
        if len(list_of_datasets) > 3:
            print('There are more then 3 Datasets here...')
            return list_of_datasets
        if len(list_of_datasets) > 1:
            test = list_of_datasets[-1]
            if len(list_of_datasets) == 3:
                valid = list_of_datasets[1]
        return Datasets(train, valid, test)

    @staticmethod
    def stack(*datasets_s):
        """
        Stack some datasets calling stack for each dataset.
        
        :param datasets_s: 
        :return: a new dataset
        """
        return Datasets.from_list([Dataset.stack(*[d[k] for d in datasets_s if d[k] is not None])
                                   for k in range(3)])


def _maybe_cast_to_scalar(what):
    return what[0] if len(what) == 1 else what


def convert_sparse_matrix_to_sparse_tensor(X):
    if isinstance(X, sc_sp.csr.csr_matrix):
        coo = X.tocoo()
        indices = np.mat([coo.row, coo.col]).transpose()
    else:
        coo, indices = X, [X.row, X.col]
    # data = np.array(coo.data, dtype=)
    return tf.SparseTensor(indices, tf.constant(coo.data, dtype=tf.float32), coo.shape)


class Dataset:
    """
    Class for managing a single dataset, includes data and target fields and has some utility functions.
     It allows also to convert the dataset into tensors and to store additional information both on a
     per-example basis and general infos.
    """

    def __init__(self, data, target, sample_info=None, info=None):
        """

        :param data: Numpy array containing data
        :param target: Numpy array containing targets
        :param sample_info: either an array of dicts or a single dict, in which case it is cast to array of
                                  dicts.
        :param info: (optional) dictionary with further info about the dataset
        """
        self._tensor_mode = False

        self._data = data
        self._target = target
        if sample_info is None:
            sample_info = {}
        self.sample_info = np.array([sample_info] * self.num_examples) \
            if isinstance(sample_info, dict) else sample_info

        assert self.num_examples == len(self.sample_info)
        assert self.num_examples == self._shape(self._target)[0]

        self.info = info or {}

    def _shape(self, what):
        return what.get_shape().as_list() if self._tensor_mode else what.shape

    def setting(self):
        """
        for save setting purposes, does not save the actual data
        
        :return: 
        """
        return {
            'num_examples': self.num_examples,
            'dim_data': self.dim_data,
            'dim_target': self.dim_target,
            'info': self.info
        }

    @property
    def data(self):
        return self._data

    @property
    def target(self):
        return self._target

    @property
    def num_examples(self):
        """

        :return: Number of examples in this dataset
        """
        return self._shape(self.data)[0]

    @property
    def dim_data(self):
        """

        :return: The data dimensionality as an integer, if input are vectors, or a tuple in the general case
        """
        return _maybe_cast_to_scalar(self._shape(self.data)[1:])

    @property
    def dim_target(self):
        """

        :return: The target dimensionality as an integer, if targets are vectors, or a tuple in the general case
        """
        shape = self._shape(self.target)
        return 1 if len(shape) == 1 else _maybe_cast_to_scalar(shape[1:])

    def convert_to_tensor(self, keep_sparse=True):
        matrices = ['_data', '_target']
        for att in matrices:
            if keep_sparse and isinstance(self.__getattribute__(att), SPARSE_SCIPY_MATRICES):
                self.__setattr__(att, convert_sparse_matrix_to_sparse_tensor(self.__getattribute__(att)))
            else:
                self.__setattr__(att, tf.convert_to_tensor(self.__getattribute__(att), dtype=tf.float32))
        self._tensor_mode = True

    def create_supplier(self, x, y, other_feeds=None):
        """
        Return a standard feed dictionary for this dataset.

        :param x: placeholder for data
        :param y: placeholder for target
        :param other_feeds: optional other feeds
        :return: a callable.
        """
        if not other_feeds: other_feeds = {}

        # noinspection PyUnusedLocal
        def _supplier(step=None):
            """

            :param step: unused, just for making it compatible with `HG` and `Saver`
            :return: the feed dictionary
            """
            if isinstance(self.data, WindowedData):
                data = self.data.generate_all()

            return {**{x: self.data, y: self.target}, **other_feeds}

        return _supplier

    @staticmethod
    def stack(*datasets):
        """
        Assuming that the datasets have same structure, stucks data and targets
        
        :param datasets: 
        :return: stacked dataset
        """
        return Dataset(data=vstack([d.data for d in datasets]),
                       target=stack_or_concat([d.target for d in datasets]),
                       sample_info=stack_or_concat([d.sample_info for d in datasets]),
                       info={k: [d.info.get(k, None) for d in datasets]
                             for k in merge_dicts(*[d.info for d in datasets])})


def to_one_hot_enc(seq, dimension=None):
    da_max = dimension or np.max(seq) + 1

    def create_and_set(_p):
        _tmp = np.zeros(da_max)
        _tmp[_p] = 1
        return _tmp

    return np.array([create_and_set(_v) for _v in seq])


def load_census():
    COLUMNS = ["age", "workclass", "fnlwgt", "education", "education_num",
               "marital_status", "occupation", "relationship", "race", "gender",
               "capital_gain", "capital_loss", "hours_per_week", "native_country",
               "income_bracket"]
    df_train = pd.read_csv(CENSUS_TRAIN, names=COLUMNS, skipinitialspace=True)
    df_test = pd.read_csv(CENSUS_TEST, names=COLUMNS, skipinitialspace=True, skiprows=1)

    LABEL_COLUMN = "label"
    df_train[LABEL_COLUMN] = (df_train["income_bracket"].apply(lambda x: ">50K" in x)).astype(int)
    df_test[LABEL_COLUMN] = (df_test["income_bracket"].apply(lambda x: ">50K" in x)).astype(int)


def load_iris(partitions_proportions=None, classes=3):
    """Loads Iris dataset divided as training and test set (by default)"""
    training_set = tf.contrib.learn.datasets.base.load_csv_with_header(
        filename=IRIS_TRAINING,
        target_dtype=np.int,
        features_dtype=np.float32)
    test_set = tf.contrib.learn.datasets.base.load_csv_with_header(
        filename=IRIS_TEST,
        target_dtype=np.int,
        features_dtype=np.float32)

    tr_set = training_set.data
    tr_targets = to_one_hot_enc(training_set.target)

    tr_dst = Dataset(data=tr_set, target=tr_targets)

    tst_set = test_set.data
    tst_targets = to_one_hot_enc(test_set.target)
    tst_dst = Dataset(data=tst_set, target=tst_targets)

    if partitions_proportions:
        if classes == 2:
            # noinspection PyUnusedLocal
            def filter_class(x, y, info, i):
                return np.argmax(y) != 0  # discard first class

            filter_list = [filter_class]

            # noinspection PyUnusedLocal
            def project_map(x, y, info, i):
                return x, y[1:], info

        else:
            filter_list, project_map = (None, None)

        res = redivide_data([tr_dst, tst_dst], partitions_proportions, filters=filter_list, maps=project_map)
        res += [None] * (3 - len(res))
        return Datasets(train=res[0], validation=res[1], test=res[2])

    return Datasets(train=tr_dst, test=tst_dst, validation=None)


def stack_or_concat(list_of_arays):
    func = np.concatenate if list_of_arays[0].ndim == 1 else np.vstack
    return func(list_of_arays)


def vstack(lst):
    """
    Vstack that considers sparse matrices
    
    :param lst: 
    :return: 
    """
    return sp.vstack(lst) if sp and isinstance(lst[0], sp.sparse.csr.csr_matrix) else np.vstack(lst)


def redivide_data(datasets, partition_proportions=None, shuffle=False, filters=None, maps=None, balance_classes=False):
    """
    Function that redivides datasets. Can be use also to shuffle or filter or map examples.

    :param datasets: original datasets, instances of class Dataset (works with get_data and get_targets for
    compatibility with mnist datasets
    :param partition_proportions: (optional, default None)  list of fractions that can either sum up to 1 or less
    then one, in which case one additional partition is created with proportion 1 - sum(partition proportions).
    If None it will retain the same proportion of samples found in datasets
    :param shuffle: (optional, default False) if True shuffles the examples
    :param filters: (optional, default None) filter or list of filters: functions with signature
    (data, target, index) -> boolean (accept or reject the sample)
    :param maps: (optional, default None) map or list of maps: functions with signature
    (data, target, index) ->  (new_data, new_target) (maps the old sample to a new one, possibly also to more
    than one sample, for data augmentation)
    :return: a list of datasets of length equal to the (possibly augmented) partition_proportion
    """

    all_data = vstack([get_data(d) for d in datasets])
    all_labels = stack_or_concat([get_targets(d) for d in datasets])

    all_infos = np.concatenate([d.sample_info for d in datasets])

    N = all_data.shape[0]

    if partition_proportions:  # argument check
        partition_proportions = list([partition_proportions] if isinstance(partition_proportions, float)
                                     else partition_proportions)
        sum_proportions = sum(partition_proportions)
        assert sum_proportions <= 1, "partition proportions must sum up to at most one: %d" % sum_proportions
        if sum_proportions < 1.: partition_proportions += [1. - sum_proportions]
    else:
        partition_proportions = [1. * get_data(d).shape[0] / N for d in datasets]

    if shuffle:
        if sp and isinstance(all_data, sp.sparse.csr.csr_matrix): raise NotImplementedError()
        # if sk_shuffle:  # TODO this does not work!!! find a way to shuffle these matrices while
        # keeping compatibility with tensorflow!
        #     all_data, all_labels, all_infos = sk_shuffle(all_data, all_labels, all_infos)
        # else:
        permutation = np.arange(all_data.shape[0])
        np.random.shuffle(permutation)

        all_data = all_data[permutation]
        all_labels = np.array(all_labels[permutation])
        all_infos = np.array(all_infos[permutation])

    if filters:
        if sp and isinstance(all_data, sp.sparse.csr.csr_matrix): raise NotImplementedError()
        filters = as_list(filters)
        data_triple = [(x, y, d) for x, y, d in zip(all_data, all_labels, all_infos)]
        for fiat in filters:
            data_triple = [xy for i, xy in enumerate(data_triple) if fiat(xy[0], xy[1], xy[2], i)]
        all_data = np.vstack([e[0] for e in data_triple])
        all_labels = np.vstack([e[1] for e in data_triple])
        all_infos = np.vstack([e[2] for e in data_triple])

    if maps:
        if sp and isinstance(all_data, sp.sparse.csr.csr_matrix): raise NotImplementedError()
        maps = as_list(maps)
        data_triple = [(x, y, d) for x, y, d in zip(all_data, all_labels, all_infos)]
        for _map in maps:
            data_triple = [_map(xy[0], xy[1], xy[2], i) for i, xy in enumerate(data_triple)]
        all_data = np.vstack([e[0] for e in data_triple])
        all_labels = np.vstack([e[1] for e in data_triple])
        all_infos = np.vstack([e[2] for e in data_triple])

    N = all_data.shape[0]
    assert N == all_labels.shape[0]

    calculated_partitions = reduce(
        lambda v1, v2: v1 + [sum(v1) + v2],
        [int(N * prp) for prp in partition_proportions],
        [0]
    )
    calculated_partitions[-1] = N

    print('datasets.redivide_data:, computed partitions numbers -',
          calculated_partitions, 'len all', N, end=' ')

    new_general_info_dict = {}
    for data in datasets:
        new_general_info_dict = {**new_general_info_dict, **data.info}

        if balance_classes:
            new_datasets = []
            forbidden_indices = np.empty(0, dtype=np.int64)
            for d1, d2 in zip(calculated_partitions[:-1], calculated_partitions[1:-1]):
                indices = np.array(get_indices_balanced_classes(d2 - d1, all_labels, forbidden_indices))
                dataset = Dataset(data=all_data[indices], target=all_labels[indices],
                                  sample_info=all_infos[indices],
                                  info=new_general_info_dict)
                new_datasets.append(dataset)
                forbidden_indices = np.append(forbidden_indices, indices)
                test_if_balanced(dataset)
            remaining_indices = np.array(list(set(list(range(N))) - set(forbidden_indices)))
            new_datasets.append(Dataset(data=all_data[remaining_indices], target=all_labels[remaining_indices],
                                        sample_info=all_infos[remaining_indices],
                                        info=new_general_info_dict))
        else:
            new_datasets = [
                Dataset(data=all_data[d1:d2], target=all_labels[d1:d2], sample_info=all_infos[d1:d2],
                        info=new_general_info_dict)
                for d1, d2 in zip(calculated_partitions, calculated_partitions[1:])
                ]

        print('DONE')

        return new_datasets


def get_indices_balanced_classes(n_examples, labels, forbidden_indices):
    N = len(labels)
    n_classes = len(labels[0])

    indices = []
    current_class = 0
    for i in range(n_examples):
        index = np.random.random_integers(0, N - 1, 1)[0]
        while index in indices or index in forbidden_indices or np.argmax(labels[index]) != current_class:
            index = np.random.random_integers(0, N - 1, 1)[0]
        indices.append(index)
        current_class = (current_class + 1) % n_classes

    return indices


def test_if_balanced(dataset):
    labels = dataset.target
    n_classes = len(labels[0])
    class_counter = [0] * n_classes
    for l in labels:
        class_counter[np.argmax(l)] += 1
    print('exemple by class: ', class_counter)


def load_20newsgroup_vectorized(folder=SCIKIT_LEARN_DATA, one_hot=True, partitions_proportions=None,
                                shuffle=False, binary_problem=False, as_tensor=True, minus_value=-1.):
    data_train = sk_dt.fetch_20newsgroups_vectorized(data_home=folder, subset='train')
    data_test = sk_dt.fetch_20newsgroups_vectorized(data_home=folder, subset='test')

    X_train = data_train.data
    X_test = data_test.data
    y_train = data_train.target
    y_test = data_test.target
    if binary_problem:
        y_train[data_train.target < 10] = minus_value
        y_train[data_train.target >= 10] = 1.
        y_test[data_test.target < 10] = minus_value
        y_test[data_test.target >= 10] = 1.
    if one_hot:
        y_train = to_one_hot_enc(y_train)
        y_test = to_one_hot_enc(y_test)

    # if shuffle and sk_shuffle:
    #     xtr = X_train.tocoo()
    #     xts = X_test.tocoo()

    d_train = Dataset(data=X_train,
                      target=y_train, info={'target names': data_train.target_names})
    d_test = Dataset(data=X_test,
                     target=y_test, info={'target names': data_train.target_names})
    res = [d_train, d_test]
    if partitions_proportions:
        res = redivide_data([d_train, d_test], partition_proportions=partitions_proportions, shuffle=False)

    if as_tensor: [dat.convert_to_tensor() for dat in res]

    return Datasets.from_list(res)


def load_realsim(folder=REALSIM, one_hot=True, partitions_proportions=None, shuffle=False, as_tensor=True):
    X, y = sk_dt.load_svmlight_file(folder + "/real-sim")
    y = np.array([int(yy) for yy in y])
    if one_hot:
        y = to_one_hot_enc(y)
    res = [Dataset(data=X, target=y)]
    if partitions_proportions:
        res = redivide_data(res, shuffle=shuffle, partition_proportions=partitions_proportions)
        res = Datasets.from_list(res)

    if as_tensor: [dat.convert_to_tensor() for dat in res]

    return res


# noinspection PyPep8Naming
def load_XRMB(folder=XRMB_DIR, half_window=2, max_speakers=100, only_independent=False, normalize_single_speaker=False):
    """
    Loads XRMB data.

    :param max_speakers:
    :param folder: path for root directory.
    :param half_window: half window size for the data.
    :param only_independent:  if False returns speaker datasets that do not keep track of the speaker.
    :param normalize_single_speaker: if True normalizes each dataset independently
    :return:    A Datasets class containing speaker independent data for training, validation and test, or a list
                a triplet of lists of Dataset if speaker_dependent is True.
    """
    prefix = folder + "/xrbm_spk_"

    set_types = ['train', 'val', 'test']

    def load_speaker(speaker_number, set_type):
        assert set_type in set_types
        files = (prefix + str(speaker_number).zfill(3) + "_%s%s.csv" % (set_type, data_type)
                 for data_type in ('audio', 'motor', 'sentences'))
        arrays = [pd.read_csv(fl, header=None).values for fl in files]
        return arrays[0], arrays[1], arrays[2] - 1  # sentence bounds are with MATLAB convetions

    def load_all_in(_range=range(1)):
        datasets = {n: [] for n in set_types}
        m, mo, sd, sto = None, None, None, None
        k = 0
        for set_type in set_types:
            for k in _range:
                try:
                    general_info_dict = {'speaker': k, 'original set': set_type}
                    data, targets, sentence_bounds = load_speaker(k, set_type)
                    if normalize_single_speaker and k != 0:  # with k = 0 use mean and sd from training set
                        data, m_sd, sd_sd = np_normalize_data(data, return_mean_and_sd=True)
                        targets, mo_sd, sto_sd = np_normalize_data(targets, return_mean_and_sd=True)
                        general_info_dict['normalizing stats'] = (m_sd, sd_sd, mo_sd, sto_sd)
                    else:
                        data, m, sd = np_normalize_data(data, m, sd, return_mean_and_sd=True)
                        targets, mo, sto = np_normalize_data(targets, mo, sto, return_mean_and_sd=True)
                        general_info_dict['normalizing stats'] = (m, sd, mo, sto)

                    data = WindowedData(data, sentence_bounds, window=half_window, process_all=True)
                    datasets[set_type].append(Dataset(data, targets,
                                                      sample_info={'speaker': k} if k != 0 else None,
                                                      info=general_info_dict))

                except OSError or FileNotFoundError:
                    k -= 1
                    break
            print('loaded %d speakers for %s' % (k, set_type))

        return datasets

    if not only_independent:
        res = load_all_in(range(0, max_speakers))

        for _set_type in set_types:  # sample-wise speaker info to the general datasets
            res[_set_type][0].sample_info_dicts = np.concatenate([
                                                                     np.array([{'speaker': k + 1}] * ds.num_examples)
                                                                     for k, ds in enumerate(res[_set_type][1:])
                                                                     ])

        return Datasets(train=res['train'], validation=res['val'], test=res['test'])
    else:
        res = load_all_in()
        return Datasets(train=res['train'][0], validation=res['val'][0], test=res['test'][0])


def load_timit_for_joint_training(folder, small=False, one_hot=True, only_gender=False):
    """

    :param folder: source folder...
    :param small: if `True` loads a smaller version of the dataset
    :param one_hot: whether to use one hot encoding for output
    :return: A list of `Datasets` where the first one is for the speaker
                dependent net and the subsequent are for group dependent nets.
                The first dataset should include validation and test data,
                while for the others (at the moment) is not needed
    """
    # # example
    # X, Y = np.array(), np.array()
    # group_id = 0
    # gender = 'M'
    # train = Dataset(X, Y, general_info_dict={'group': group_id, 'gender': gender})
    # datasets = Datasets(train=train)

    if small:
        set_names = ['train_small', 'validation_small', 'coretest_small']
    else:
        set_names = ['train', 'validation', 'coretest']
    Xall = {}
    Yall = {}
    datasets = [None]
    for gender in ['F', 'M']:
        _temp_gender = []
        for dr in range(1, 9):
            sets = []

            for s in set_names:
                # Loading data
                fname = '{}_DR{}_{}.npy'.format(s, dr, gender)
                data = np.load(os.path.join(folder, fname))
                # Creating dataset
                X = data[:, :-1]
                Y = data[:, -1]
                if one_hot:
                    Y = to_one_hot_enc(np.array(Y, dtype=np.int32), dimension=183)
                info = {'group': dr, 'gender': gender}
                sets.append(Dataset(X, Y, info=info))
                # Stacking data for full dataset
                Xall[s] = np.vstack((Xall[s], X)) if s in Xall else X
                if one_hot:
                    Yall[s] = np.vstack((Yall[s], Y)) if s in Yall else Y
                else:
                    Yall[s] = np.hstack((Yall[s], Y)) if s in Yall else Y
            ds = Datasets(train=sets[0], validation=sets[1], test=sets[2])
            if not only_gender:
                datasets.append(ds)
            else:
                _temp_gender.append(ds)
        if only_gender:
            datasets.append(Datasets.stack(*_temp_gender))
    # Building full dataset
    # sets = []
    # for s in set_names:
    #     sets.append(Dataset(Xall[s], Yall[s]))
    # ds = Datasets(train=sets[0], validation=sets[1], test=sets[2])
    # datasets[0] = ds
    datasets[0] = Datasets.stack(*datasets[1:])

    return datasets


# noinspection PyUnusedLocal
def load_timit(folder=TIMIT_DIR, only_primary=False, filters=None, maps=None, small=False, context=None,
               fake=False, process_all=False):
    def load_timit_sentence_bound():
        def sentence_bound_reader(name):
            bnd = pd.read_csv(folder + '/timit_%sSentenceBound.csv' % name, header=None).values
            return bnd - 1

        return [sentence_bound_reader(n) for n in ['train', 'val', 'test']]

    folder = folder or TIMIT_DIR
    if isinstance(process_all, bool):
        process_all = [process_all] * 3

    if fake:
        def generate_dataset(secondary=False):
            target = np.random.randn(2000, 183)
            if secondary:
                target = np.hstack([target, np.random.randn(2000, 300)])
            return np.random.randn(2000, 123), target

        training_data, training_target = generate_dataset(not only_primary)
        validation_data, validation_target = generate_dataset()
        test_data, test_target = generate_dataset()
        training_info_dict = None
    else:
        split_number = '00' if small else ''
        training_target = pd.read_csv(folder + '/timit_trainTargets%s.csv' % split_number, header=None).values
        training_data = pd.read_csv(folder + '/timit-preproc_traindata_norm_noctx%s.csv' %
                                    split_number, header=None).values
        training_info_dict = {'dim_primary_target': training_target.shape[1]}
        print('loaded primary training data')
        if not only_primary:
            training_secondary_target = pd.read_csv(folder + '/timit_trainTargetsPE%s.csv'
                                                    % split_number, header=None).values
            training_target = np.hstack([training_target, training_secondary_target])
            training_info_dict['dim_secondary_target'] = training_secondary_target.shape[1]
            print('loaded secondary task targets')

        validation_data = pd.read_csv(folder + '/timit-preproc_valdata_norm_noctx%s.csv'
                                      % split_number, header=None).values
        validation_target = pd.read_csv(folder + '/timit_valTargets%s.csv' % split_number, header=None).values
        print('loaded validation data')

        test_data = pd.read_csv(folder + '/timit-preproc_testdata_norm_noctx.csv', header=None).values
        test_target = pd.read_csv(folder + '/timit_testTargets.csv', header=None).values
        print('loaded test data')

    if context:
        sbs = load_timit_sentence_bound()
        training_data, validation_data, test_data = (WindowedData(d, s, context, process_all=pa) for d, s, pa
                                                     in zip([training_data, validation_data, test_data],
                                                            sbs, process_all))

    test_dataset = Dataset(data=test_data, target=test_target)
    validation_dataset = Dataset(data=validation_data, target=validation_target)
    training_dataset = Dataset(data=training_data, target=training_target, info=training_info_dict)

    res = Datasets(train=training_dataset, validation=validation_dataset, test=test_dataset)

    return res


def load_mnist(folder=None, one_hot=True, partitions=None, filters=None, maps=None, shuffle=False):
    if not folder: folder = MNIST_DIR
    datasets = read_data_sets(folder, one_hot=one_hot)
    train = Dataset(datasets.train.images, datasets.train.labels)
    validation = Dataset(datasets.validation.images, datasets.validation.labels)
    test = Dataset(datasets.test.images, datasets.test.labels)
    res = [train, validation, test]
    if partitions:
        res = redivide_data(res, partition_proportions=partitions, filters=filters, maps=maps, shuffle=shuffle)
        res += [None] * (3 - len(res))
    return Datasets.from_list(res)


def load_caltech101_30(folder=CALTECH101_30_DIR, tiny_problem=False):
    caltech = scio.loadmat(folder + '/caltech101-30.matlab')
    k_train, k_test = caltech['Ktrain'], caltech['Ktest']
    label_tr, label_te = caltech['tr_label'], caltech['te_label']
    file_tr, file_te = caltech['tr_files'], caltech['te_files']

    if tiny_problem:
        pattern_step = 5
        fraction_limit = 0.2
        k_train = k_train[:int(len(label_tr) * fraction_limit):pattern_step,
                  :int(len(label_tr) * fraction_limit):pattern_step]
        label_tr = label_tr[:int(len(label_tr) * fraction_limit):pattern_step]

    U, s, Vh = linalg.svd(k_train)
    S_sqrt = linalg.diagsvd(s ** 0.5, len(s), len(s))
    X = np.dot(U, S_sqrt)  # examples in rows

    train_x, val_x, test_x = X[0:len(X):3, :], X[1:len(X):3, :], X[2:len(X):3, :]
    label_tr_enc = to_one_hot_enc(np.array(label_tr) - 1)
    train_y, val_y, test_y = label_tr_enc[0:len(X):3, :], label_tr_enc[1:len(X):3, :], label_tr_enc[2:len(X):3, :]
    train_file, val_file, test_file = file_tr[0:len(X):3], file_tr[1:len(X):3], file_tr[2:len(X):3]

    test_dataset = Dataset(data=test_x, target=test_y, info={'files': test_file})
    validation_dataset = Dataset(data=val_x, target=val_y, info={'files': val_file})
    training_dataset = Dataset(data=train_x, target=train_y, info={'files': train_file})

    return Datasets(train=training_dataset, validation=validation_dataset, test=test_dataset)


def load_iros15(folder=IROS15_BASE_FOLDER, resolution=15, legs='all', part_proportions=(.7, .2), one_hot=True,
                shuffle=True):
    resolutions = (5, 11, 15)
    legs_names = ('LF', 'LH', 'RF', 'RH')
    assert resolution in resolutions
    folder += str(resolution)
    if legs == 'all': legs = legs_names
    base_name_by_leg = lambda leg: os.path.join(folder, 'trainingSet%sx%sFromSensor%s.mat'
                                                % (resolution, resolution, leg))

    datasets = {}
    for _leg in legs:
        dat = scio.loadmat(base_name_by_leg(_leg))
        data, target = dat['X'], to_one_hot_enc(dat['Y']) if one_hot else dat['Y']
        # maybe pre-processing??? or it is already done? ask...
        datasets[_leg] = Datasets.from_list(
            redivide_data([Dataset(data, target, info={'leg': _leg})],
                          partition_proportions=part_proportions, shuffle=shuffle))
    return datasets


def load_caltech101(folder=CALTECH101_DIR, one_hot=True, partitions=None, filters=None, maps=None):
    path = folder + "/caltech101.pickle"
    with open(path, "rb") as input_file:
        X, target_name, files = cpickle.load(input_file)
    dict_name_ID = {}
    i = 0
    list_of_targets = sorted(list(set(target_name)))
    for k in list_of_targets:
        dict_name_ID[k] = i
        i += 1
    dict_ID_name = {v: k for k, v in dict_name_ID.items()}
    Y = []
    for name_y in target_name:
        Y.append(dict_name_ID[name_y])
    if one_hot:
        Y = to_one_hot_enc(Y)
    dataset = Dataset(data=X, target=Y, info={'dict_name_ID': dict_name_ID, 'dict_ID_name': dict_ID_name},
                      sample_info=[{'target_name': t, 'files': f} for t, f in zip(target_name, files)])
    if partitions:
        res = redivide_data([dataset], partitions, filters=filters, maps=maps, shuffle=True)
        res += [None] * (3 - len(res))
        return Datasets(train=res[0], validation=res[1], test=res[2])
    return dataset


def load_cifar10(folder=CIFAR10_DIR, one_hot=True, partitions=None, filters=None, maps=None, balance_classes=False):
    path = folder + "/cifar-10.pickle"
    with open(path, "rb") as input_file:
        X, target_name, files = cpickle.load(input_file)
    X = np.array(X)
    dict_name_ID = {}
    i = 0
    list_of_targets = sorted(list(set(target_name)))
    for k in list_of_targets:
        dict_name_ID[k] = i
        i += 1
    dict_ID_name = {v: k for k, v in dict_name_ID.items()}
    Y = []
    for name_y in target_name:
        Y.append(dict_name_ID[name_y])
    if one_hot:
        Y = to_one_hot_enc(Y)
    dataset = Dataset(data=X, target=Y, info={'dict_name_ID': dict_name_ID, 'dict_ID_name': dict_ID_name},
                      sample_info=[{'target_name': t, 'files': f} for t, f in zip(target_name, files)])
    if partitions:
        res = redivide_data([dataset], partitions, filters=filters, maps=maps, shuffle=True, balance_classes=True)
        res += [None] * (3 - len(res))
        return Datasets(train=res[0], validation=res[1], test=res[2])
    return dataset


def load_cifar100(folder=CIFAR100_DIR, one_hot=True, partitions=None, filters=None, maps=None):
    path = folder + "/cifar-100.pickle"
    with open(path, "rb") as input_file:
        X, target_ID_fine, target_ID_coarse, fine_ID_corr, coarse_ID_corr, files = cpickle.load(input_file)
    X = np.array(X);

    target_ID_fine = target_ID_fine[:len(X)]
    target_ID_coarse = target_ID_coarse[:len(X)]

    fine_ID_corr = {v: k for v, k in zip(range(len(fine_ID_corr)), fine_ID_corr)}
    coarse_ID_corr = {v: k for v, k in zip(range(len(coarse_ID_corr)), coarse_ID_corr)}
    fine_label_corr = {v: k for k, v in fine_ID_corr.items()}
    coarse_label_corr = {v: k for k, v in coarse_ID_corr.items()}

    Y = []
    for name_y in target_ID_fine:
        Y.append(name_y)
    Y = np.array(Y)
    if one_hot:
        Y = to_one_hot_enc(Y)
    superY = []
    for name_y in target_ID_coarse:
        superY.append(name_y)
    superY = np.array(superY)
    if one_hot:
        superY = to_one_hot_enc(superY)

    print(len(X))
    print(len(Y))
    dataset = Dataset(data=X, target=Y,
                      info={'dict_name_ID_fine': fine_label_corr, 'dict_name_ID_coarse': coarse_label_corr,
                                         'dict_ID_name_fine': fine_ID_corr, 'dict_ID_name_coarse': coarse_ID_corr},
                      sample_info=[{'Y_coarse': yc, 'files': f} for yc, f in zip(superY, files)])
    if partitions:
        res = redivide_data([dataset], partitions, filters=filters, maps=maps, shuffle=True)
        res += [None] * (3 - len(res))
        return Datasets(train=res[0], validation=res[1], test=res[2])
    return dataset


def generate_multiclass_dataset(n_samples=100, n_features=10,
                                n_informative=5, n_redundant=3, n_repeated=2,
                                n_classes=2, n_clusters_per_class=2,
                                weights=None, flip_y=0.01, class_sep=1.0,
                                hypercube=True, shift=0.0, scale=1.0,
                                shuffle=True, random_state=None, hot_encoded=True, partitions_proportions=None,
                                negative_labels=-1.):
    X, y = sk_dt.make_classification(n_samples=n_samples, n_features=n_features,
                                     n_informative=n_informative, n_redundant=n_redundant, n_repeated=n_repeated,
                                     n_classes=n_classes, n_clusters_per_class=n_clusters_per_class,
                                     weights=weights, flip_y=flip_y, class_sep=class_sep,
                                     hypercube=hypercube, shift=shift, scale=scale,
                                     shuffle=True, random_state=random_state)
    if hot_encoded:
        y = to_one_hot_enc(y)
    else:
        y[y == 0] = negative_labels
    res = Dataset(data=np.array(X, dtype=np.float32), target=np.array(y, dtype=np.float32),
                  info={'n_informative': n_informative, 'n_redundant': n_redundant,
                                     'n_repeated': n_repeated,
                                     'n_classes': n_classes, 'n_clusters_per_class': n_clusters_per_class,
                                     'weights': weights, 'flip_y': flip_y, 'class_sep': class_sep,
                                     'hypercube': hypercube, 'shift': shift, 'scale': scale,
                                     'shuffle': True, 'random_state': random_state})
    np.random.seed(random_state)
    if partitions_proportions:
        res = redivide_data([res], shuffle=shuffle, partition_proportions=partitions_proportions)
        res = Datasets.from_list(res)
    return res


def get_data(d_set):
    if hasattr(d_set, 'images'):
        data = d_set.images
    elif hasattr(d_set, 'data'):
        data = d_set.data
    else:
        raise ValueError("something wrong with the dataset %s" % d_set)
    return data


def get_targets(d_set):
    if hasattr(d_set, 'labels'):
        return d_set.labels
    elif hasattr(d_set, 'target'):
        return d_set.target
    else:
        raise ValueError("something wrong with the dataset %s" % d_set)


#
class ExampleVisiting:
    def __init__(self, dataset, batch_size, epochs=None):
        """
        Class for stochastic sampling of data points. It is most useful for feeding examples for the the
        training ops of `ReverseHG` or `ForwardHG`. Most notably, if the number of epochs is specified,
        the class takes track of the examples per mini-batches which is important for the backward pass
        of `ReverseHG` method.

        :param dataset: instance of `Dataset` class
        :param batch_size:
        :param epochs: number of epochs (can be None, in which case examples are
                        fed continuously)
        """
        self.dataset = dataset
        self.batch_size = batch_size
        self.epochs = epochs
        self.T = int(np.ceil(dataset.num_examples / batch_size))
        if self.epochs: self.T *= self.epochs

        self.training_schedule = None
        self.iter_per_epoch = int(dataset.num_examples / batch_size)

    def setting(self):
        excluded = ['training_schedule', 'datasets']
        dictionary = {k: v for k, v in vars(self).items() if k not in excluded}
        if hasattr(self.dataset, 'setting'):
            dictionary['dataset'] = self.dataset.setting()
        return dictionary

    def generate_visiting_scheme(self):
        """
        Generates and stores example visiting scheme, as a numpy array of integers.

        :return: self
        """

        def all_indices_shuffled():
            _res = list(range(self.dataset.num_examples))
            np.random.shuffle(_res)
            return _res

        # noinspection PyUnusedLocal
        self.training_schedule = np.concatenate([all_indices_shuffled()
                                                 for _ in range(self.epochs or 1)])
        return self

    def create_supplier(self, x, y, other_feeds=None, lambda_feeds=None):
        return self.create_feed_dict_supplier(x, y, other_feeds=other_feeds,
                                              lambda_feeds=lambda_feeds)

    def create_feed_dict_supplier(self, x, y, other_feeds=None, lambda_feeds=None):
        """

        :param x: placeholder for independent variable
        :param y: placeholder for dependent variable
        :param lambda_feeds: dictionary of placeholders: number_of_example -> substitution
        :param other_feeds: dictionary of other feeds (e.g. dropout factor, ...) to add to the input output
                            feed_dict
        :return: a function that generates a feed_dict with the right signature for Reverse and Forward HyperGradient
                    classes
        """

        if not lambda_feeds:
            lambda_processed_feeds = {}
        if not other_feeds:
            other_feeds = {}

        def _training_supplier(step=None):
            nonlocal lambda_processed_feeds, other_feeds

            if step >= self.T:
                if step % self.T == 0:
                    if self.epochs:
                        print('WARNING: End of the training scheme reached.'
                              'Generating another scheme.')
                    self.generate_visiting_scheme()
                step %= self.T

            if self.training_schedule is None:
                # print('visiting scheme not yet generated!')
                self.generate_visiting_scheme()

            # noinspection PyTypeChecker
            nb = self.training_schedule[step * self.batch_size: min(
                (step + 1) * self.batch_size, len(self.training_schedule))]

            bx = self.dataset.data[nb, :]
            by = self.dataset.target[nb, :]
            if lambda_feeds:
                lambda_processed_feeds = {k: v(nb) for k, v in lambda_feeds.items()}
            else:
                lambda_processed_feeds = {}
            return {**{x: bx, y: by}, **other_feeds, **lambda_processed_feeds}

        return _training_supplier


def pad(_example, _size): return np.concatenate([_example] * _size)


class WindowedData(object):
    def __init__(self, data, row_sentence_bounds, window=5, process_all=False):
        """
        Class for managing windowed input data (like TIMIT).

        :param data: Numpy matrix. Each row should be an example data
        :param row_sentence_bounds:  Numpy matrix with bounds for padding. TODO add default NONE
        :param window: half-window size
        :param process_all: (default False) if True adds context to all data at object initialization.
                            Otherwise the windowed data is created in runtime.
        """
        self.window = window
        self.data = data
        base_shape = self.data.shape
        self.shape = (base_shape[0], (2 * self.window + 1) * base_shape[1])
        self.tree = it.IntervalTree([it.Interval(int(e[0]), int(e[1]) + 1) for e in row_sentence_bounds])
        if process_all:
            print('adding context to all the dataset', end='- ')
            self.data = self.generate_all()
            print('DONE')
        self.process_all = process_all

    def generate_all(self):
        return self[:]

    def __getitem__(self, item):  # TODO should be right for all the common use... But better write down a TestCase
        if hasattr(self, 'process_all') and self.process_all:  # keep attr check!
            return self.data[item]
        if isinstance(item, int):
            return self.get_context(item=item)
        if isinstance(item, tuple):
            if len(item) == 2:
                rows, columns = item
                if isinstance(rows, int) and isinstance(columns, int):  # TODO check here
                    # do you want the particular element?
                    return self.get_context(item=rows)[columns]
            else:
                raise TypeError('NOT IMPLEMENTED <|>')
            if isinstance(rows, slice):
                rows = range(*rows.indices(self.shape[0]))
            return np.vstack([self.get_context(r) for r in rows])[:, columns]
        else:
            if isinstance(item, slice):
                item = range(*item.indices(self.shape[0]))
            return np.vstack([self.get_context(r) for r in item])

    def __len__(self):
        return self.shape[0]

    def get_context(self, item):
        interval = list(self.tree[item])[0]
        # print(interval)
        left, right = interval[0], interval[1]
        left_pad = max(self.window + left - item, 0)
        right_pad = max(0, self.window - min(right, len(self) - 1) + item)  # this is to cope with reduce datasets
        # print(left, right, item)

        # print(left_pad, right_pad)
        base = np.concatenate(self.data[item - self.window + left_pad: item + self.window + 1 - right_pad])
        if left_pad:
            base = np.concatenate([pad(self.data[item], left_pad), base])
        if right_pad:
            base = np.concatenate([base, pad(self.data[item], right_pad)])
        return base
#
#
# if __name__ == '__main__':
#     # _datasets = load_20newsgroup_feed_vectorized(one_hot=False, binary_problem=True)
#     # print(_datasets.train.dim_data)
#     # print(_datasets.train.dim_target)
#     # mnist = load_mnist(partitions=[0.1, .2], filters=lambda x, y, d, k: True)
#     # print(len(_datasets.train))\
#
#     load_20newsgroup_vectorized(one_hot=False, shuffle=True, partitions_proportions=(1 / 3, 1 / 3))
#
#     mnist = load_mnist(partitions=(.1, .1), shuffle=True)
#
#     print(mnist.train.data)
#     print(type(mnist.train.data))
#
#     # dt = load_20newsgroup_vectorized()
#     # print(dt.train.num_examples)
#     # print(dt.train.num_examples)
