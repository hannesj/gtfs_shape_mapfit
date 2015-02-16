#!/usr/bin/python2
import os
import sys
import time
import datetime
import math

from osgeo import osr
from osgeo import ogr

import pymapmatch.osmmapmatch as omm
from common import read_gtfs_shapes, GtfsShapeWriter
from collections import defaultdict
import itertools

class ShapeError(Exception): pass

ROUTE_TYPE_FILTERS = {
	'0': "TRAM_FILTER",
	'1': "SUBWAY_FILTER",
	'3': "BUSWAY_FILTER",
	'2': "TRAIN_FILTER",
	'109': "TRAIN_FILTER", # Non-standard type for Helsinki's data
}

from threading import Lock, RLock, Thread
from Queue import Queue
stderr_lock = Lock()
def stderr(*args):
	with stderr_lock:
		print >>sys.stderr, ' '.join(args)

def gtfs_shape_mapfit(map_file, projection, whitelist=None, search_region=130.0, node_ids=False):
	
	#if whitelist:
	#	whitelist = set(whitelist.split(','))
	#def gfile(fname):
	#	return open(os.path.join(gtfs_directory, fname))
	#routes = read_gtfs_routes(gfile('routes.txt'))
	shapeFile = ogr.Open('linjaukset.shp')
	layer = shapeFile.GetLayer()

	destSrs = osr.SpatialReference()
	destSrs.ImportFromEPSG(4326)
	#shape_trips = read_gtfs_shape_trips(gfile('trips.txt'))
	#def shape_route_type(shape_id):
		#return '3'
		#route_ids = set(t.route_id for t in shape_trips[shape_id])
		#if not route_ids:
		#	return None
		#types = set(routes[r_id].route_type for r_id in route_ids)
		#if len(types) != 1:
		#	raise ShapeError("Multiple route types for shape %s!"%(shape_id))
		#return types.pop()
	
	def getShapes():
		for line in iter(layer):
			line.GetGeometryRef().TransformTo(destSrs)
			points = line.GetGeometryRef().GetPoints()
			lon, lat = zip(*points)[0:2]
			yield (line.GetFieldAsString("VUORO_LISA"), zip(lat, lon)) 

	projection = omm.CoordinateProjector(projection)
	
	def sync(method):
		def synced(self, *args, **kwargs):
			with self.lock:
				stuff = method(self, *args, **kwargs)
			return stuff
		return synced

	class Graphs(defaultdict):
		def __init__(self):
			self.lock = RLock()
		
		__getitem__ = sync(defaultdict.__getitem__)
		__setitem__ = sync(defaultdict.__setitem__)
		__contains__ = sync(defaultdict.__contains__)

		def __missing__(self, type_filter):
			if type_filter is None:
				#print >>sys.stderr, "No map filter for route type %s"%route_type
				self[type_filter] = None
				return None
			filt = getattr(omm, type_filter)
			#stderr("Loading graph for %s"%type_filter)
			graph = omm.OsmGraph(map_file, projection, filt)
			self[type_filter] = graph
			return graph
	graphs = Graphs()
	
	from multiprocessing.pool import ThreadPool
	def do_fit(shape):
		shape_id, shape_coords = shape
		route_type = '3' #shape_route_type(shape_id)
		type_filter = ROUTE_TYPE_FILTERS.get(route_type)
		graph = graphs[type_filter]
		if graph is None:
			return shape_id, shape_coords, [], [], None, None

		state_model = omm.DrawnGaussianStateModel(30, 0.05, graph)
		matcher = omm.MapMatcher2d(graph, state_model, search_region)
		
		coords = [projection(*c) for c in shape_coords]
		points = [omm.Point2d(*c) for c in coords]
		times = [0.0]*len(points)
		matcher.measurements(times, points)
		#for c in coords:
		#	matcher.measurement(0, *c)
		fitted_coords = [(p.x, p.y) for p in matcher.best_match_coordinates() if not math.isnan(p.x) and not math.isnan(p.y)]
		fitted_nodes = [p for p in matcher.best_match_node_ids()]
		#stderr(str(fitted_nodes))
		#stderr(str(fitted_coords))
		fitted = [projection.inverse(*c) for c in fitted_coords]
		
		states = []
		state = matcher.best_current_hypothesis()
		while state:
			states.append(state)
			state = state.parent
		
		return shape_id, fitted, fitted_nodes, states, matcher, type_filter
	
	#shapes = list(shapes)
	shapes = getShapes()
	#if whitelist:
	#	shapes = [s for s in shapes if s[0] in whitelist]
	
	start_time = time.time()
	#results = (do_fit(s) for s in shapes)
	extra_cols = []
	#if node_ids:
	#	extra_cols.append('node_id')
	
	shape_writer = GtfsShapeWriter(sys.stdout, *extra_cols)
	for i, (shape_id, shape_coords, ids, states, matcher, type_filter) in enumerate(do_fit(s) for s in shapes):
		likelihoods = [s.measurement_likelihood+s.transition_likelihood for s in states]
		time_spent = time.time() - start_time
		mean_time = time_spent/float(i+1)
		time_left = mean_time*(11884-i)
		status = "Shape %i/%i, approx %s left"%(i+1, 11884, datetime.timedelta(seconds=time_left))
		if len(likelihoods) == 0:
			minlik = None
			n_outliers = 0
		else:
			minlik = min(likelihoods)
			n_outliers = matcher.n_outliers
		logrow = shape_id, minlik, n_outliers, type_filter, status
		stderr(';'.join(map(str, logrow)))
		
		extra_cols = []
		if node_ids:
			ids = [p if p > 0 else "" for p in ids]
			extra_cols.append(ids)
		shape_writer(shape_id, shape_coords, *extra_cols)

if __name__ == '__main__':
	import argh
	argh.dispatch_command(gtfs_shape_mapfit)
