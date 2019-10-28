"""
Support for TIFF imagery.
"""

from abc import ABC, abstractmethod
import math
from multiprocessing.dummy import Pool as ThreadPool
import sys
import os

import numpy as np

from delta.imagery import image_reader
from delta.imagery import rectangle
from delta.imagery import tfrecord_utils

# TODO: Not currently used, but could be if the TF method of filtering chunks is inefficient.
def parallel_filter_chunks(data, num_threads):
    """Filter out chunks that contain the Landsat nodata value (zero)"""

    (num_chunks, unused_num_bands, width, height) = data.shape()
    num_chunk_pixels = width * height

    valid_chunks = [True] * num_chunks
    splits = []
    thread_size = float(num_chunks) / float(num_threads)
    for i in range(0,num_threads):
        start_index = math.floor(i    *thread_size)
        stop_index  = math.floor((i+1)*thread_size)
        splits.append((start_index, stop_index))

    # Internal function to flag nodata chunks from the start to stop indices (non-inclusive)
    def check_chunks(pair):
        (start_index, stop_index) = pair
        for i in range(start_index, stop_index):
            chunk = data[i, 0, :, :]
            print(chunk.shape())
            print(chunk)
            if np.count_nonzero(chunk) != num_chunk_pixels:
                valid_chunks[i] = False
                print('INVALID')

    # Call check_chunks in parallel using a thread pool
    pool = ThreadPool(num_threads)
    pool.map(check_chunks, splits)
    pool.close()
    pool.join()

    # Remove the bad chunks
    valid_indices = []
    for i in range(0,num_chunks):
        if valid_chunks[i]:
            valid_indices.append(i)

    print('Num remaining chunks = ' + str(len(valid_indices)))

    return data[valid_indices, :, :, :]

def horizontal_split(image_size, region, num_splits):
    """Return the ROI of an image to load given the region.
       Each region represents one horizontal band of the image.
    """

    assert region < num_splits, 'Input region ' + str(region) \
           + ' is greater than num_splits: ' + str(num_splits)

    min_x = 0
    max_x = image_size[0]

    # Fractional height here is fine
    band_height = image_size[1] / num_splits

    # TODO: Check boundary conditions!
    min_y = math.floor(band_height*region)
    max_y = math.floor(band_height*(region+1.0))

    return rectangle.Rectangle(min_x, min_y, max_x, max_y)

def tile_split(image_size, region, num_splits):
    """Return the ROI of an image to load given the region.
       Each region represents one tile in a grid split.
    """
    num_tiles = num_splits*num_splits
    assert region < num_tiles, 'Input region ' + str(region) \
           + ' is greater than num_tiles: ' + str(num_tiles)

    # Convert region index to row and column index
    tile_row = math.floor(region / num_splits)
    tile_col = region % num_splits

    # Fractional sizes are fine here
    tile_width  = math.floor(image_size[0] / num_splits)
    tile_height = math.floor(image_size[1] / num_splits)

    # TODO: Check boundary conditions!
    min_x = math.floor(tile_width  * tile_col)
    max_x = math.floor(tile_width  * (tile_col+1.0))
    min_y = math.floor(tile_height * tile_row)
    max_y = math.floor(tile_height * (tile_row+1.0))

    return rectangle.Rectangle(min_x, min_y, max_x, max_y)


class DeltaImage(ABC):
    """Base class used for wrapping input images in a way that they can be passed
       to Tensorflow dataset objects.
    """

    DEFAULT_EXTENSIONS = ['.tif']

    # Constants which must be specified for all image types, these are the default values.
    def __init__(self, num_regions):
        self._num_regions = num_regions

    @abstractmethod
    def chunk_image_region(self, roi, chunk_size, chunk_overlap, data_type=np.float64):
        """Return this portion of the image broken up into small segments.
           The output format is a numpy array of size [N, num_bands, chunk_size, chunk_size]
        """

    @abstractmethod
    def read(self, data_type=np.float64, roi=None):
        """
        Read the image of the given data type. An optional roi specifies the boundaries.
        """

    @abstractmethod
    def size(self):
        """Return the size of this image in pixels"""

    @abstractmethod
    def prep(self):
        """Prepare the file to be opened by other tools (unpack, etc)"""

    # TODO: to num_bands
    @abstractmethod
    def get_num_bands(self):
        """Return the number of bands in the image"""

    def tiles(self):
        """Generator to yield ROIs for the image."""
        s = self.size()
        # TODO: add to config, replace with max buffer size?
        for i in range(self._num_regions):
            yield horizontal_split(s, i, self._num_regions)

    def estimate_memory_usage(self, chunk_size, chunk_overlap, data_type=np.float64,
                              num_bands=0):
        """Estimate the memory needed to chunk one region in bytes.
           Assumes horizontal regions, overwrite if using a different method."""
        if num_bands < 1:
            num_bands = self.get_num_bands()
        full_size  = self.size()
        height     = full_size[1] / self._num_regions
        num_pixels = height * full_size[0]
        spacing    = chunk_size - chunk_overlap
        num_chunks = num_pixels / (spacing*spacing)
        chunk_area = chunk_size * chunk_size
        return num_chunks * chunk_area * num_bands * sys.getsizeof(data_type(0))

class TFRecordImage(DeltaImage):
    def __init__(self, path, _, num_regions):
        super(TFRecordImage, self).__init__(num_regions)
        self.path = path
        self._num_bands = None
        self._size = None

    def prep(self):
        pass
    def chunk_image_region(self, roi, chunk_size, chunk_overlap, data_type=np.float64):
        pass

    def read(self, data_type=np.float64, roi=None):
        raise NotImplementedError()

    def __get_bands_size(self):
        self._num_bands, width, height = tfrecord_utils.get_record_info(self.path)
        self._size = (width, height)

    def get_num_bands(self):
        if self._num_bands is None:
            self.__get_bands_size()
        return self._num_bands

    def size(self):
        if self._size is None:
            self.__get_bands_size()
        return self._size

class TiffImage(DeltaImage):
    """For all versions of DeltaImage that can use our image_reader class"""

    DEFAULT_EXTENSIONS = ['.tif']

    def __init__(self, path, cache_manager, num_regions):
        super(TiffImage, self).__init__(num_regions)
        self.path = path
        self._cache_manager = cache_manager

    @abstractmethod
    def prep(self):
        pass

    def get_num_bands(self):
        """Return the number of bands in a prepared file"""
        input_paths = self.prep()

        input_reader = image_reader.MultiTiffFileReader()
        input_reader.load_images(input_paths)
        return input_reader.num_bands()

    def chunk_image_region(self, roi, chunk_size, chunk_overlap, data_type=np.float64):
        # First make sure that the image is unpacked and ready to load
        input_paths = self.prep()

        # Set up the input image handle
        input_reader = image_reader.MultiTiffFileReader()
        input_reader.load_images(input_paths)

        # Load the chunks from inside the ROI
        #print('Loading chunk data from file ' + self.path + ' using ROI: ' + str(roi))
        # TODO: configure number of threads
        chunk_data = input_reader.parallel_load_chunks(roi, chunk_size, chunk_overlap, 1, data_type=data_type)

        return chunk_data

    def read(self, data_type=np.float64, roi=None):
        input_paths = self.prep()

        # Set up the input image handle
        input_reader = image_reader.MultiTiffFileReader()
        input_reader.load_images(input_paths)
        if roi is None:
            s = input_reader.image_size()
            roi = rectangle.Rectangle(0, 0, s[0], s[1])
        roi = rectangle.Rectangle(int(roi.min_x), int(roi.min_y), int(roi.max_x), int(roi.max_y))
        return input_reader.read_roi(roi, data_type=data_type)

    def size(self):
        input_paths = self.prep()

        input_reader = image_reader.MultiTiffFileReader()
        input_reader.load_images(input_paths)
        return input_reader.image_size()


class SimpleTiff(TiffImage):
    """A basic image which comes ready to use"""
    _NUM_REGIONS = 1
    DEFAULT_EXTENSIONS = ['.tif']

    def prep(self):
        return [self.path]


class RGBAImage(TiffImage):
    """Basic RGBA images where the alpha channel needs to be stripped"""

    _NUM_REGIONS = 1
    DEFAULT_EXTENSIONS = ['.tif']

    def prep(self):
        """Converts RGBA images to RGB images"""

        # Get the path to the cached image
        fname = os.path.basename(self.path)
        output_path = self._cache_manager.register_item(fname)

        if not os.path.exists(output_path):
            # Just remove the alpha band from the original image
            cmd = 'gdal_translate -b 1 -b 2 -b 3 ' + self.path + ' ' + output_path
            print(cmd)
            os.system(cmd)
        return [output_path]
