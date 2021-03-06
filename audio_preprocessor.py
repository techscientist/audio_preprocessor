
import os, sys
import json
import csv
import numpy as np
from sklearn import preprocessing
import h5py
import librosa
from joblib import Parallel, delayed
import multiprocessing as mp
import pdb
# Constant
N_JOBS=mp.cpu_count()
# os.environ['OPENBLAS_NUM_THREADS'] = '1' # https://github.com/librosa/librosa/issues/381#issuecomment-229850344

# TODO: segment HDF, add logging. and perhaps that's it?

class Audio_Preprocessor():
	def __init__(self, settings_path=None):
		''' 
		settings_path: json file path for settings (use settings.json if None)
		'''
		if settings_path is None:
			settings_path = 'settings.json'
		self.settings_path = settings_path
		with open(settings_path) as json_file:
			settings = json.load(json_file)

		self.config = settings["config"]
		self.load = settings["load"]
		self.transforms = settings["transforms"]
		self.labels = settings["labels"]
		
		self.paths = None # paths sorted by index
		self.num_files = 0
		self.permutations = None
		self.hdf_path = os.path.join(self.config['write']['result_root_path'], self.config['write']['name']+'.hdf')

	def init_paths(self):
		'''
		search path to set self.paths,
		and then permute them, store them as in csv, and 
		'''
		self.__index()
		self.__get_permutations()
		self.__save_csv()

	def __index(self, overwrite=False):
		''' 
		Find every audio file that has allowed extensions (in self.config['read']['exts'] recursively.)
		Then, store them as a list in self.paths
		'''
		def find_audio_file(path):
			'''find audio file/path recursively.'''
			filenames = os.listdir(path)
			audio_filepaths = [os.path.join(path, f) for f in filenames \
				if os.path.splitext(f)[-1].lower().lstrip('.') in self.config['read']['exts']]
			subfolder_paths = [os.path.join(path, f) for f in filenames \
				if os.path.isdir(os.path.join(path, f))]
			for f in audio_filepaths:
				self.paths.append(f)

			for f in subfolder_paths:
				find_audio_file(f)
			return
		''''''
		if overwrite is False:
			if self.paths is not None:
				return

		self.paths = []
		find_audio_file(self.config['read']['source_root_path'])
		self.num_files = len(self.paths)
		return

	def __get_permutations(self):
		''' 
		Shuffle the index.
		use sklearn for better valid set.
		have csv files of [path][labels]
		'''
		np.random.seed(1209)
		self.permutations = np.random.permutation(len(self.paths))
		return

	def __save_csv(self):
		csv_path = self.config['read']['permuted_csv_path']
		with open(csv_path, 'wb') as f_csv:
			csv_writer = csv.writer(f_csv, delimiter='\t')
			for path_idx in xrange(len(self.paths)):
				csv_writer.writerow([self.permutations[path_idx], self.paths[self.permutations[path_idx]]])

	def __gen_permuted_path(self, n_yield=1):
		csv_path = self.config['read']['permuted_csv_path']
		with open(csv_path, 'rb') as f_csv:
			csv_reader = csv.reader(f_csv, delimiter='\t')
			for row in csv_reader:
				yield row[1]

	def __open_hdf(self):
		''' 
		Create or open a new hdf file. 
		'''		
		try:
			os.mkdir(self.config['write']['result_root_path'])
		except:
			if os.path.exists(self.config['write']['result_root_path']):
				print('... There is already the output folder at %s' % os.path.exists(self.config['write']['result_root_path']))
				pass
			else:
				raise RuntimeError('Failed to create or find the result folder, %s' % self.config['write']['result_root_path'])
		try:
			f = h5py.File(self.hdf_path, 'r+')
		except IOError:	
			f = h5py.File(self.hdf_path, 'w')
		return f

	def __get_args(self, name):
		''' 
		Get function, args, kwargs for a certain transform
		Usually convert_* function use it.

		name: string, name of the transform e.g. 'melgram', 'stft', 'cqt'...
		'''
		sr = self.load['sr']
		dct = self.transforms[name] # settings dictionary
		args, kwargs = [], {}
		if name == 'melgram':
			func = librosa.feature.melspectrogram
			args = [sr, None, dct['n_fft'], dct['hop_length']]
			kwargs = {'n_mels':dct['n_mels']}
		elif name == 'cqt':
			func = librosa.core.cqt
			args = [sr, dct['hop_length'], None, dct['n_bins'], dct['bins_per_octave']]
		elif name == 'stft':
			func = librosa.core.stft
			args = [dct['n_fft'], dct['hop_length']]
		else:
			raise RuntimeError('wrong transform name : %s' % name)
		return func, args, kwargs

	def __get_size_x(self, name, example_x):
		''' 
		Get the size of input
		name: string, e.g. 'melgram', 'stft', ...
		example_x = input audio signal. 
		Return type does not include the number of data or channels, i.e. (height, width)
		'''
		sr = self.load['sr']
		func, args, kwargs = self.__get_args(name)
		return func(example_x, *args, **kwargs).shape
	
	def __get_size_y(self, name):
		''' 
		Get size of the output. not implemented yet.
		'''
		return
	
	def convert_one_transform(self, transform, multiprocessing=True):
		''' 
		Get size of the transform, make hdf dataset, close it, and then fetch processes

		transform: string, 'melgram' e.g.
		'''
		def store_to_hdf(hdf_handler, idxs, data):
			''' hdf_handler: actually a dataset handler, which enables a direct writing
			idxs: tuple, (idx_from, idx_to)
			data: a list of data to write
			'''
			hdf_handler[idxs[0]:idxs[1]] = np.array(data)
			return
		''''''
		# prepare hdf handler, function args,...
		f = self.__open_hdf()
		load_args = [self.load['sr'], True, self.load['offset'], self.load['duration'], np.float32]
		example_x, sr = librosa.load(self.paths[0], *load_args)
		size = (self.num_files, 1, ) + self.__get_size_x(transform, example_x) # n_data, n_ch, height, width
		func, args, kwargs = self.__get_args(transform)
		f.require_dataset(transform, size, dtype=np.float32)
		f.close()
		# do the work.
		n_chunks = 3
		f = self.__open_hdf()
		f_write = f[transform]
		path_iterator = self.__gen_permuted_path()
		
		if multiprocessing:
			for path_batch_idx in xrange(int(np.ceil(float(len(self.paths))/n_chunks))):
				idx_from = path_batch_idx*n_chunks
				idx_to = (path_batch_idx+1)*n_chunks
				paths_to_process = []
				for i in range(n_chunks):
					try:
						paths_to_process.append(next(path_iterator))
					except:
						break
				
				transform_results = Parallel(n_jobs=n_chunks)(delayed(convert_one_item)(i_data, audio_path, transform, load_args, func, args, kwargs, 
											self.transforms[transform]['logam'], self.transforms[transform]['normalize']) \
										for i_data, audio_path in enumerate(paths_to_process))
				# print 'ddd', len(transform_results), transform_results[0].shape
				store_to_hdf(f_write, (idx_from, idx_to), transform_results)
		else:
			for i_data, path in enumerate(self.paths):
				transform_result = convert_one_item(i_data, path, transform, load_args, func, args, kwargs, 
						self.transforms[transform]['logam'], self.transforms[transform]['normalize']) 
				store_to_hdf(f_write, (i_data, i_data+1), [transform_result])
		
	def convert_all(self):
		'''
		get the all transforms that are specified in settings.json

		'''
		if sys.platform == 'darwin': # for developing purpose
			for transform in self.transforms: # text keys e.g. 'melgram'
				if transform != 'melgram':
					self.convert_one_transform(transform)
		else:
			for transform in self.transforms: # text keys e.g. 'melgram'
				self.convert_one_transform(transform)
		return


def convert_one_item(i_data, path, transform, load_args, func, args, kwargs, is_logam=True, is_normalize=True):
	''' 
	A function that is called by joblib, and therfore outside the class.
	It converts the signal AND put the result into the hdf at the hdf_path 
	'''
	print('transform:%s for %s' % (transform, path))
	x, sr = librosa.load(path, *load_args) # load, always mono
	X = func(x, *args, **kwargs) # process
	X = np.abs(X)
	if is_logam:
		X = librosa.logamplitude(X)
	if is_normalize:
		X = preprocessing.scale(X)
	X = np.expand_dims(X, axis=0) # to make it (n_channel, n_freq, n_frame) and n_channel==1
	return X
