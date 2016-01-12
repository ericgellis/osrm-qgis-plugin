# -*- coding: utf-8 -*-
"""
osrm_utils
----------
Utilities function used for the plugin.

----------

Contains two pieces of code for which i'm not the author :
 - lru_cache functionnality, written by Raymond Hettinger (MIT licence, 2012)
 - PolylineCodec class, written by Bruno M. Custodio (MIT licence, 2014)

"""
import numpy as np
from itertools import islice
from PyQt4.QtGui import QColor, QFileDialog, QDialog
from PyQt4.QtCore import QSettings, QFileInfo
from qgis.core import (
    QgsGeometry, QgsPoint, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsSymbolV2, QgsMessageLog
    )
from qgis.gui import QgsEncodingFileDialog
from matplotlib.pyplot import contourf
from matplotlib.mlab import griddata
from httplib import HTTPConnection
from .osrm_utils_extern import PolylineCodec, lru_cache
import json

__all__ = ['check_host', 'save_dialog', 'save_dialog_geo',
           'qgsgeom_from_mpl_collec', 'prepare_route_symbol',
           'interpolate_from_times', 'get_coords_ids', 'chunk_it',
           'accumulated_time', 'pts_ref', 'TemplateOsrm',
           'return_osrm_table_version', 'decode_geom', 'h_light_table',
           'rectangular_light_table', 'decode_geom_to_pts', 'h_locate',
           'make_regular_points', 'get_search_frame', 'get_isochrones_colors']


class TemplateOsrm(object):
    """
    Template class to be subclassed by each OSRM dialog class.
    It contains some methods used by the five next class.
    """
    def display_error(self, error, code):
        msg = {
            1: "An error occured when trying to contact the OSRM instance",
            2: "OSRM plugin error report : Too many errors occured "
               "when trying to contact the OSRM instance at {} - "
               "Route calculation has been stopped".format(self.host),
            }
        self.iface.messageBar().pushMessage(
            "Error", msg[code] + "(see QGis log for error traceback)",
            duration=10)
        QgsMessageLog.logMessage(
            'OSRM-plugin error report :\n {}'.format(error),
            level=QgsMessageLog.WARNING)

    @lru_cache(maxsize=25)
    def query_url(self, url, host):
        """
        LRU cached function to make request to OSRM instance.
        """
        self.conn.request('GET', url, headers=self.http_headers)
        parsed = json.loads(self.conn.getresponse().read().decode('utf-8'))
        return parsed

    def store_origin(self, point):
        """
        Method to store a click on the QGIS canvas
        """
        if '4326' not in self.canvas.mapSettings().destinationCrs().authid():
            crsSrc = self.canvas.mapSettings().destinationCrs()
            xform = QgsCoordinateTransform(
                crsSrc, QgsCoordinateReferenceSystem(4326))
            point = xform.transform(point)
        self.origin = point
        self.canvas.unsetMapTool(self.originEmit)
        self.lineEdit_xyO.setText(str(point))


def check_host(url):
    """ Helper function to get the hostname in desired format """
    if len(url) < 4:
        raise ValueError('Probably empty/non-valable url')
    if not ('http' in url and '//' in url) and url[-1] == '/':
        host = url[:-1]
    elif not ('http:' in url and '//' in url):
        host = url
    elif 'http://' in url[:7] and url[-1] == '/':
        host = url[7:-1]
    elif 'http://' in url[:7]:
        host = url[7:]
    else:
        host = url
    return host


def save_dialog(filtering="CSV (*.csv *.CSV)"):
    settings = QSettings()
    dirName = settings.value("/UI/lastShapefileDir")
    encode = settings.value("/UI/encoding")
    fileDialog = QgsEncodingFileDialog(
        None, "Save output csv", dirName, filtering, encode
        )
    fileDialog.setDefaultSuffix('csv')
    fileDialog.setFileMode(QFileDialog.AnyFile)
    fileDialog.setAcceptMode(QFileDialog.AcceptSave)
    fileDialog.setConfirmOverwrite(True)
    if not fileDialog.exec_() == QDialog.Accepted:
        return None, None
    files = fileDialog.selectedFiles()
    settings.setValue("/UI/lastShapefileDir",
                      QFileInfo(unicode(files[0])).absolutePath())
    return (unicode(files[0]), unicode(fileDialog.encoding()))


def save_dialog_geo(filtering="ESRI Shapefile (*.shp *.SHP)"):
    settings = QSettings()
    dirName = settings.value("/UI/lastShapefileDir")
    encode = settings.value("/UI/encoding")
    fileDialog = QgsEncodingFileDialog(
        None, "Save output ShapeFile", dirName, filtering, encode
        )
    fileDialog.setDefaultSuffix('shp')
    fileDialog.setFileMode(QFileDialog.AnyFile)
    fileDialog.setAcceptMode(QFileDialog.AcceptSave)
    fileDialog.setConfirmOverwrite(True)
    if not fileDialog.exec_() == QDialog.Accepted:
        return None, None
    files = fileDialog.selectedFiles()
    settings.setValue("/UI/lastShapefileDir",
                      QFileInfo(unicode(files[0])).absolutePath())
    return (unicode(files[0]), unicode(fileDialog.encoding()))


def prepare_route_symbol(nb_route):
    colors = ['#1f78b4', '#ffff01', '#ff7f00',
              '#fb9a99', '#b2df8a', '#e31a1c']
    p = nb_route % len(colors)
    my_symb = QgsSymbolV2.defaultSymbol(1)
    my_symb.setColor(QColor(colors[p]))
    my_symb.setWidth(1.2)
    return my_symb


def qgsgeom_from_mpl_collec(collections):
    polygons = []
    for i, polygon in enumerate(collections):
        mpoly = []
        for path in polygon.get_paths():
            path.should_simplify = False
            poly = path.to_polygons()
            if len(poly) > 0 and len(poly[0]) > 4:
                exterior = [QgsPoint(*p.tolist()) for p in poly[0]]
                holes = [
                    [QgsPoint(*p.tolist()) for p in h]
                    for h in poly[1:] if len(h) > 4
                    ]
                if len(holes) == 1:
                    mpoly.append([exterior, holes[0]])
                elif len(holes) > 1:
                    mpoly.append([exterior] + [h for h in holes])
                else:
                    mpoly.append([exterior, holes])
        if len(mpoly) > 1:
            polygons.append(QgsGeometry.fromMultiPolygon(mpoly))
        elif len(mpoly) == 1:
            polygons.append(QgsGeometry.fromPolygon(mpoly[0]))
    return polygons


def interpolate_from_times(times, coords, levels):
    x = np.array([coordx[0] for coordx in coords])
    y = np.array([coordy[1] for coordy in coords])
    xi = np.linspace(np.nanmin(x), np.nanmax(x), 200)
    yi = np.linspace(np.nanmin(y), np.nanmax(y), 200)
    zi = griddata(x, y, times, xi, yi, interp='linear')
    collec_poly = contourf(
        xi, yi, zi, levels, vmax=abs(zi).max(), vmin=-abs(zi).max())
    return collec_poly


def get_coords_ids(layer, field):
    if '4326' not in layer.crs().authid():
        xform = QgsCoordinateTransform(
            layer.crs(), QgsCoordinateReferenceSystem(4326))
        coords = [xform.transform(ft.geometry().asPoint())
                  for ft in layer.getFeatures()]
    else:
        coords = [ft.geometry().asPoint() for ft in layer.getFeatures()]

    if field != '':
        ids = [ft.attribute(field) for ft in layer.getFeatures()]
    else:
        ids = [ft.id() for ft in layer.getFeatures()]

    return coords, ids


def chunk_it(it, size):
    it = iter(it)
    return list(iter(lambda: tuple(islice(it, size)), ()))


def accumulated_time(features):
    res = [0]
    for feature in features:
        res.append(feature[4] + res[len(res) - 1])
    return res


def pts_ref(features):
    return [i[3] for i in features]


def return_osrm_table_version(host, coord, headers=None):
    try:
        conn = HTTPConnection(host)
        url = '/locate?loc={}'.format(str(coord[1]) + ',' + str(coord[0]))
        conn.request('GET', url, headers=headers)
        parsed_json = json.loads(conn.getresponse().read().decode('utf-8'))
        if parsed_json['status'] == 400:
            return 'new'
        else:
            return 'old'
    except Exception as err:
        print(err)
        return 'error'


def decode_geom(encoded_polyline):
    """
    Function decoding an encoded polyline (with 'encoded polyline
    algorithme') and returning a QgsGeometry object

    Params:

    encoded_polyline: str
        The encoded string to decode
    """
    return QgsGeometry.fromPolyline(
        [QgsPoint(i[0], i[1]) for i
         in PolylineCodec().decode(encoded_polyline)])


def h_light_table(list_coords, conn, headers=None):
    """
    Function wrapping OSRM 'table' function in order to get a matrix of
    time distance as a numpy array

    Params :

        list_coords: list
            A list of coord as (x, y) , like :
                 list_coords = [(21.3224, 45.2358),
                                (21.3856, 42.0094),
                                (20.9574, 41.5286)] (coords have to be float)
        conn: httplib.HTTPConnection object

    Output:
        - a numpy array containing the time in tenth of seconds
            (where 2147483647 means not-found route)

        -1 is return in case of any other error (bad 'output' parameter,
            wrong list of coords/ids, unknow host,
            wrong response from the host, etc.)
    """
    query = ['/table?loc=']
    for coord in list_coords:  # Preparing the query
        if coord:
            tmp = ''.join([str(coord[1]), ',', str(coord[0]), '&loc='])
            query.append(tmp)
    query = (''.join(query))[:-5]

    try:  # Querying the OSRM instance
        conn.request('GET', query, headers=headers)
        parsed_json = json.loads(conn.getresponse().read().decode('utf-8'))
    except Exception as err:
        raise ValueError('Error while contacting OSRM instance : \n{}'
                                  .format(err))

    if 'distance_table' in parsed_json.keys():  # Preparing the result matrix
        mat = np.array(parsed_json['distance_table'], dtype='int32')
        if len(mat) < len(list_coords):
            print('The array returned by OSRM is smaller than the array '
                  'requested\nOSRM parameter --max-table-size should be'
                  ' increased')
            raise ValueError(
                  'The array returned by OSRM is smaller than the array '
                  'requested\nOSRM parameter --max-table-size should be'
                  ' increased')
        else:
            return mat
    else:
        raise ValueError('No distance table return by OSRM instance')


def rectangular_light_table(src_coords, dest_coords, conn, headers=None):
    """
    Function wrapping new OSRM 'table' function in order to get a rectangular
    matrix of time distance as a numpy array

    Params :

        src_coords: list
            A list of coord as (x, y) , like :
                 list_coords = [(21.3224, 45.2358),
                                (21.3856, 42.0094),
                                (20.9574, 41.5286)] (coords have to be float)
        dest_coords: list
            A list of coord as (x, y) , like :
                 list_coords = [(21.3224, 45.2358),
                                (21.3856, 42.0094),
                                (20.9574, 41.5286)] (coords have to be float)
        conn: httplib.HTTPConnection object
        headers: dict
            headers (as dict) to be transmited to the
            httplib.HTTPConnection.request function.

    Output:
        - a numpy array containing the time in tenth of seconds
            (where 2147483647 means not-found route)
        - a numpy array of snapped source coordinates (lat, lng)
        - a numpy array of snapped destination coordinates (lat, lng)

        ValueError is raised in case of any error
            (wrong list of coords/ids, unknow host,
            wrong response from the host, etc.)
    """
    query = ['/table?src=']
    # If only one source code (not nested) :
    if len(src_coords) == 2 and not isinstance(src_coords[0], (list, tuple, QgsPoint)):
        query.append(''.join(
            [str(src_coords[1]), ',', str(src_coords[0]), '&dst=']))
    else: # Otherwise :
        for coord in src_coords:  # Preparing the query
            if coord:
                tmp = ''.join([str(coord[1]), ',', str(coord[0]), '&src='])
                query.append(tmp)
        query[-1] = query[-1][:-5] + '&dst='

    if len(dest_coords) == 2 and not isinstance(dest_coords[0], (list, tuple, QgsPoint)):
        tmp = ''.join([str(dest_coords[1]), ',', str(dest_coords[0]), '&dst='])
        query.append(tmp)
    else:
        for coord in dest_coords:  # Preparing the query
            if coord:
                tmp = ''.join([str(coord[1]), ',', str(coord[0]), '&dst='])
                query.append(tmp)

    query = (''.join(query))[:-5]
    try:  # Querying the OSRM instance
        conn.request('GET', query, headers=headers)
        parsed_json = json.loads(conn.getresponse().read().decode('utf-8'))
    except Exception as err:
        raise ValueError('Error while contacting OSRM instance : \n{}'
                                  .format(err))

    if 'distance_table' in parsed_json.keys():  # Preparing the result matrix
        mat = np.array(parsed_json['distance_table'], dtype='int32')
        src_snapped = np.array(parsed_json['source_coordinates'], dtype='float64')
        dest_snapped = np.array(parsed_json['destination_coordinates'], dtype='float64')
        return mat, src_snapped, dest_snapped
    else:
        raise ValueError('No distance table return by OSRM instance')


def decode_geom_to_pts(encoded_polyline):
    """
    Params:

    encoded_polyline: str
        The encoded string to decode
    """
    return [(i[0], i[1]) for i in PolylineCodec().decode(encoded_polyline)]


def h_locate(coord, conn, headers=None):
    """
    Useless function wrapping OSRM 'locate' function,
    returning the reponse in JSON.
    More useless since newer version of OSRM doesn't include 'locate' function
    anymore.

    Parameters
    ----------
    coord: list/tuple of two floats
        (x ,y) where x is longitude and y is latitude
    host: str, default 'http://localhost:5000'
        Url and port of the OSRM instance (no final bakslash)

    Return
    ------
       The JSON returned by the OSRM instance
    """
    url = '/locate?loc={}'.format(str(coord[1]) + ',' + str(coord[0]))
    try:  # Querying the OSRM instance
        conn.request('GET', url, headers=headers)
        parsed_json = json.loads(conn.getresponse().read().decode('utf-8'))
        if 'mapped_coordinate' in parsed_json:
            return parsed_json
        else:
            return {'mapped_coordinate': (None,)}
    except Exception as err:
        return {'mapped_coordinate': (None,)}


def make_regular_points(bounds, nb_pts):
    """
    Return a square grid of regular points (same number in height and width
    even if the bbox is not a square).
    """
    xmin, ymin, xmax, ymax = bounds
    nb_h = int(round(np.sqrt(nb_pts)))
    prog_x = [xmin + i * ((xmax - xmin) / nb_h) for i in range(nb_h + 1)]
    prog_y = [ymin + i * ((ymax - ymin) / nb_h) for i in range(nb_h + 1)]
    res = []
    for x in prog_x:
        for y in prog_y:
            res.append((x, y))
    return res
#    return [(x, y) for x in prog_x for y in prog_y]


def get_search_frame(point, max_time):
    search_len = (max_time * 2.25) * 1000
    crsSrc = QgsCoordinateReferenceSystem(4326)
    xform = QgsCoordinateTransform(crsSrc, QgsCoordinateReferenceSystem(3857))
    point = xform.transform(QgsPoint(*point))
    xmin = point[0] - search_len
    ymin = point[1] - search_len
    xmax = point[0] + search_len
    ymax = point[1] + search_len
    crsSrc = QgsCoordinateReferenceSystem(3857)
    xform = QgsCoordinateTransform(crsSrc, QgsCoordinateReferenceSystem(4326))
    xmin, ymin = xform.transform(QgsPoint(xmin, ymin))
    xmax, ymax = xform.transform(QgsPoint(xmax, ymax))
    return xmin, ymin, xmax, ymax


def get_isochrones_colors(nb_features):
    """ Ugly "helper" function to rewrite to avoid repetitions """
    return {1: ('#a6d96a'),
            2: ('#fee08b', '#a6d96a'),
            3: ('#66bd63',
                '#fee08b', '#f46d43'),
            4: ('#1a9850', '#a6d96a',
                '#fee08b', '#f46d43'),
            5: ('#1a9850', '#66bd63',
                '#ffffbf', '#fc8d59', '#d73027'),
            6: ('#1a9850', '#66bd63', '#d9ef8b',
                '#fee08b', '#fc8d59', '#d73027'),
            7: ('#1a9850', '#66bd63', '#d9ef8b', '#ffffbf',
                '#fee08b', '#fc8d59', '#d73027'),
            8: ('#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                '#fee08b', '#fdae61', '#f46d43', '#d73027'),
            9: ('#1a9850', '#66bd63', '#a6d96a', '#d9ef8b', '#ffffbf',
                '#fee08b', '#fdae61', '#f46d43', '#d73027'),
            10: ('#006837', '#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                 '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026'),
            11: ('#006837', '#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                 '#ffffbf', '#fee08b', '#fdae61', '#f46d43', '#d73027',
                 '#a50026'),
            12: ('#006837', '#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                 '#e7ef88', '#ffffbf', '#fee08b', '#fdae61', '#f46d43',
                 '#d73027', '#a50026'),
            13: ('#006837', '#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                 '#e7ef88', '#ffffbf', '#fee08b', '#fdae61', '#f46d43',
                 '#d73027', '#bb2921', '#a50026'),
            14: ('#006837', '#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                 '#e7ef88', '#ffffbf', '#fff6a0', '#fee08b', '#fdae61',
                 '#f46d43', '#d73027', '#bb2921', '#a50026'),
            15: ('#006837', '#1a9850', '#66bd63', '#a6d96a', '#d9ef8b',
                 '#e7ef88', '#ffffbf', '#ffffbf', '#fff6a0', '#fee08b',
                 '#fdae61', '#f46d43', '#d73027', '#bb2921', '#a50026'),
            16: ('#006837', '#1a9850', '#66bd63', '#a6d96a',
                 '#d9ef8b', '#e7ef88', '#ffffbf', '#ffffbf', '#ffffbf',
                 '#fff6a0', '#fee08b', '#fdae61', '#f46d43', '#d73027',
                 '#bb2921', '#a50026'),
            }[nb_features]


#
################################################################################
##
##    Ligthweighted copy of the Polyline Codec for Python
##    (https://pypi.python.org/pypi/polyline)
##    realeased under MIT licence, 2014, by Bruno M. Custodio :
##
################################################################################
#
#class PolylineCodec(object):
#    """
#    Copy of the "_trans" and "decode" functions
#    from the Polyline Codec (https://pypi.python.org/pypi/polyline) released
#    under MIT licence, 2014, by Bruno M. Custodio.
#    """
#    def _trans(self, value, index):
#        byte, result, shift = None, 0, 0
#        while (byte is None or byte >= 0x20):
#            byte = ord(value[index]) - 63
#            index += 1
#            result |= (byte & 0x1f) << shift
#            shift += 5
#            comp = result & 1
#        return ~(result >> 1) if comp else (result >> 1), index
#
#    def decode(self, expression):
#        coordinates, index, lat, lng, length = [], 0, 0, 0, len(expression)
#        while (index < length):
#            lat_change, index = self._trans(expression, index)
#            lng_change, index = self._trans(expression, index)
#            lat += lat_change
#            lng += lng_change
#            coordinates.append((lng / 1e6, lat / 1e6))
#        return coordinates
#
################################################################################
##    Full featured backport from functools.lru_cache for python 2.7.
##
##    Verbatim copy of the recipe published by :
##            Raymond Hettinger (MIT licence, 2012)
##    on ActiveState : http://code.activestate.com/recipes/578078/
################################################################################
#
#_CacheInfo = namedtuple("CacheInfo", ["hits", "misses", "maxsize", "currsize"])
#
#
#class _HashedSeq(list):
#    __slots__ = 'hashvalue'
#
#    def __init__(self, tup, hash=hash):
#        self[:] = tup
#        self.hashvalue = hash(tup)
#
#    def __hash__(self):
#        return self.hashvalue
#
#
#def _make_key(args, kwds, typed,
#              kwd_mark=(object(),),
#              fasttypes={int, str, frozenset, type(None)},
#              sorted=sorted, tuple=tuple, type=type, len=len):
#    'Make a cache key from optionally typed positional and keyword arguments'
#    key = args
#    if kwds:
#        sorted_items = sorted(kwds.items())
#        key += kwd_mark
#        for item in sorted_items:
#            key += item
#    if typed:
#        key += tuple(type(v) for v in args)
#        if kwds:
#            key += tuple(type(v) for k, v in sorted_items)
#    elif len(key) == 1 and type(key[0]) in fasttypes:
#        return key[0]
#    return _HashedSeq(key)
#
#
#def lru_cache(maxsize=100, typed=False):
#    """Least-recently-used cache decorator.
#
#    If *maxsize* is set to None, the LRU features are disabled and the cache
#    can grow without bound.
#
#    If *typed* is True, arguments of different types will be cached separately.
#    For example, f(3.0) and f(3) will be treated as distinct calls with
#    distinct results.
#
#    Arguments to the cached function must be hashable.
#
#    View the cache statistics named tuple (hits, misses, maxsize, currsize) with
#    f.cache_info().  Clear the cache and statistics with f.cache_clear().
#    Access the underlying function with f.__wrapped__.
#
#    See:  http://en.wikipedia.org/wiki/Cache_algorithms#Least_Recently_Used
#
#    """
#
#    # Users should only access the lru_cache through its public API:
#    #       cache_info, cache_clear, and f.__wrapped__
#    # The internals of the lru_cache are encapsulated for thread safety and
#    # to allow the implementation to change (including a possible C version).
#
#    def decorating_function(user_function):
#
#        cache = dict()
#        stats = [0, 0]                  # make statistics updateable non-locally
#        HITS, MISSES = 0, 1             # names for the stats fields
#        make_key = _make_key
#        cache_get = cache.get           # bound method to lookup key or return None
#        _len = len                      # localize the global len() function
#        lock = RLock()                  # because linkedlist updates aren't threadsafe
#        root = []                       # root of the circular doubly linked list
#        root[:] = [root, root, None, None]      # initialize by pointing to self
#        nonlocal_root = [root]                  # make updateable non-locally
#        PREV, NEXT, KEY, RESULT = 0, 1, 2, 3    # names for the link fields
#
#        if maxsize == 0:
#
#            def wrapper(*args, **kwds):
#                # no caching, just do a statistics update after a successful call
#                result = user_function(*args, **kwds)
#                stats[MISSES] += 1
#                return result
#
#        elif maxsize is None:
#
#            def wrapper(*args, **kwds):
#                # simple caching without ordering or size limit
#                key = make_key(args, kwds, typed)
#                result = cache_get(key, root)   # root used here as a unique not-found sentinel
#                if result is not root:
#                    stats[HITS] += 1
#                    return result
#                result = user_function(*args, **kwds)
#                cache[key] = result
#                stats[MISSES] += 1
#                return result
#
#        else:
#
#            def wrapper(*args, **kwds):
#                # size limited caching that tracks accesses by recency
#                key = make_key(args, kwds, typed) if kwds or typed else args
#                with lock:
#                    link = cache_get(key)
#                    if link is not None:
#                        # record recent use of the key by moving it to the front of the list
#                        root, = nonlocal_root
#                        link_prev, link_next, key, result = link
#                        link_prev[NEXT] = link_next
#                        link_next[PREV] = link_prev
#                        last = root[PREV]
#                        last[NEXT] = root[PREV] = link
#                        link[PREV] = last
#                        link[NEXT] = root
#                        stats[HITS] += 1
#                        return result
#                result = user_function(*args, **kwds)
#                with lock:
#                    root, = nonlocal_root
#                    if key in cache:
#                        # getting here means that this same key was added to the
#                        # cache while the lock was released.  since the link
#                        # update is already done, we need only return the
#                        # computed result and update the count of misses.
#                        pass
#                    elif _len(cache) >= maxsize:
#                        # use the old root to store the new key and result
#                        oldroot = root
#                        oldroot[KEY] = key
#                        oldroot[RESULT] = result
#                        # empty the oldest link and make it the new root
#                        root = nonlocal_root[0] = oldroot[NEXT]
#                        oldkey = root[KEY]
#                        oldvalue = root[RESULT]
#                        root[KEY] = root[RESULT] = None
#                        # now update the cache dictionary for the new links
#                        del cache[oldkey]
#                        cache[key] = oldroot
#                    else:
#                        # put result in a new link at the front of the list
#                        last = root[PREV]
#                        link = [last, root, key, result]
#                        last[NEXT] = root[PREV] = cache[key] = link
#                    stats[MISSES] += 1
#                return result
#
#        def cache_info():
#            """Report cache statistics"""
#            with lock:
#                return _CacheInfo(stats[HITS], stats[MISSES], maxsize, len(cache))
#
#        def cache_clear():
#            """Clear the cache and cache statistics"""
#            with lock:
#                cache.clear()
#                root = nonlocal_root[0]
#                root[:] = [root, root, None, None]
#                stats[:] = [0, 0]
#
#        wrapper.__wrapped__ = user_function
#        wrapper.cache_info = cache_info
#        wrapper.cache_clear = cache_clear
#        return update_wrapper(wrapper, user_function)
#
#    return decorating_function
#
################################################################################
################################################################################
