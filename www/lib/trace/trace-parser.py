#!/usr/bin/python
"""
Copyright 2016 Google Inc. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import gzip
import logging
import math
import os
import time

# try a fast json parser if it is installed
try:
  import ujson as json
except:
  import json

########################################################################################################################
#   Trace processing
########################################################################################################################
class Trace():
  def __init__(self):
    self.thread_stack = {}
    self.ignore_threads = {}
    self.threads = {}
    self.user_timing = []
    self.event_names = {}
    self.event_name_lookup = {}
    self.scripts = None
    self.timeline_events = []
    self.start_time = None
    self.end_time = None
    self.cpu = {'main_thread': None}
    self.feature_usage = None
    self.feature_usage_start_time = None
    self.netlog = {'bytes_in': 0, 'bytes_out': 0}
    return

  ########################################################################################################################
  #   Output Logging
  ########################################################################################################################
  def WriteJson(self, file, json_data):
    try:
      file_name, ext = os.path.splitext(file)
      if ext.lower() == '.gz':
        with gzip.open(file, 'wb') as f:
          json.dump(json_data, f)
      else:
        with open(file, 'w') as f:
          json.dump(json_data, f)
    except:
      logging.critical("Error writing to " + file)

  def WriteUserTiming(self, file):
    self.WriteJson(file, self.user_timing)

  def WriteCPUSlices(self, file):
    self.WriteJson(file, self.cpu)

  def WriteScriptTimings(self, file):
    if self.scripts is not None:
      self.WriteJson(file, self.scripts)

  def WriteFeatureUsage(self, file):
    self.WriteJson(file, self.feature_usage)

  def WriteNetlog(self, file):
    self.WriteJson(file, self.netlog)


  ########################################################################################################################
  #   Top-level processing
  ########################################################################################################################
  def Process(self, trace, load_all):
    f = None
    line_mode = False
    self.__init__()
    try:
      if load_all:
        with gzip.open(trace,'rb') as trace_file:
          trace_events = json.load(trace_file);
          for trace_event in trace_events['traceEvents']:
            self.ProcessTraceEvent(trace_event)
      else:
        file_name, ext = os.path.splitext(trace)
        if ext.lower() == '.gz':
          f = gzip.open(trace, 'rb')
        else:
          f = open(trace, 'r')
        for line in f:
          try:
            trace_event = json.loads(line.strip("\r\n\t ,"))
            if not line_mode and 'traceEvents' in trace_event:
              for sub_event in trace_event['traceEvents']:
                self.ProcessTraceEvent(sub_event)
            else:
              line_mode = True
              self.ProcessTraceEvent(trace_event)
          except:
            pass
    except:
      logging.critical("Error processing trace " + trace)

    if f is not None:
      f.close()
    self.ProcessTimelineEvents()

  def ProcessTraceEvent(self, trace_event):
    cat = trace_event['cat']
    if cat == 'devtools.timeline' or cat.find('devtools.timeline') >= 0:
      self.ProcessTimelineTraceEvent(trace_event)
    elif cat.find('blink.feature_usage') >= 0:
      self.ProcessFeatureUsageEvent(trace_event)
    elif cat.find('blink.user_timing') >= 0:
      self.user_timing.append(trace_event)
    #Netlog support is still in progress
    #elif cat.find('netlog') >= 0:
    #  self.ProcessNetlogEvent(trace_event)


  ########################################################################################################################
  #   Timeline
  ########################################################################################################################
  def ProcessTimelineTraceEvent(self, trace_event):
    thread = '{0}:{1}'.format(trace_event['pid'], trace_event['tid'])

    # Keep track of the main thread
    if self.cpu['main_thread'] is None and trace_event['name'] == 'ResourceSendRequest' and 'args' in trace_event and \
            'data' in trace_event['args'] and 'url' in trace_event['args']['data']:
      if trace_event['args']['data']['url'][:21] == 'http://127.0.0.1:8888':
        self.ignore_threads[thread] = True
      else:
        if thread not in self.threads:
          self.threads[thread] = {}
        if self.start_time is None or trace_event['ts'] < self.start_time:
          self.start_time = trace_event['ts']
        self.cpu['main_thread'] = thread
        if 'dur' not in trace_event:
          trace_event['dur'] = 1

    # Make sure each thread has a numerical ID
    if self.cpu['main_thread'] is not None and thread not in self.threads and thread not in self.ignore_threads and \
            trace_event['name'] != 'Program':
      self.threads[thread] = {}

    # Build timeline events on a stack. 'B' begins an event, 'E' ends an event
    if (thread in self.threads and ('dur' in trace_event or trace_event['ph'] == 'B' or trace_event['ph'] == 'E')):
      trace_event['thread'] = self.threads[thread]
      if thread not in self.thread_stack:
        self.thread_stack[thread] = []
      if trace_event['name'] not in self.event_names:
        self.event_names[trace_event['name']] = len(self.event_names)
        self.event_name_lookup[self.event_names[trace_event['name']]] = trace_event['name']
      if trace_event['name'] not in self.threads[thread]:
        self.threads[thread][trace_event['name']] = self.event_names[trace_event['name']]
      e = None
      if trace_event['ph'] == 'E':
        if len(self.thread_stack[thread]) > 0:
          e = self.thread_stack[thread].pop()
          if e['n'] == self.event_names[trace_event['name']]:
            e['e'] = trace_event['ts']
      else:
        e = {'t': thread, 'n': self.event_names[trace_event['name']], 's': trace_event['ts']}
        if (trace_event['name'] == 'EvaluateScript' or trace_event['name'] == 'v8.compile' or trace_event['name'] == 'v8.parseOnBackground')\
                and 'args' in trace_event and 'data' in trace_event['args'] and 'url' in trace_event['args']['data'] and\
                trace_event['args']['data']['url'].startswith('http'):
          e['js'] = trace_event['args']['data']['url']
        if trace_event['name'] == 'FunctionCall' and 'args' in trace_event and 'data' in trace_event['args'] and\
                'scriptName' in trace_event['args']['data'] and trace_event['args']['data']['scriptName'].startswith('http'):
          e['js'] = trace_event['args']['data']['scriptName']
        if trace_event['ph'] == 'B':
          self.thread_stack[thread].append(e)
          e = None
        elif 'dur' in trace_event:
          e['e'] = e['s'] + trace_event['dur']

      if e is not None and 'e' in e and e['s'] >= self.start_time and e['e'] >= e['s']:
        if self.end_time is None or e['e'] > self.end_time:
          self.end_time = e['e']
        # attach it to a parent event if there is one
        if len(self.thread_stack[thread]) > 0:
          parent = self.thread_stack[thread].pop()
          if 'c' not in parent:
            parent['c'] = []
          parent['c'].append(e)
          self.thread_stack[thread].append(parent)
        else:
          self.timeline_events.append(e)

  def ProcessTimelineEvents(self):
    if len(self.timeline_events) and self.end_time > self.start_time:
      # Figure out how big each slice should be in usecs. Size it to a power of 10 where we have at least 2000 slices
      exp = 0
      last_exp = 0
      slice_count = self.end_time - self.start_time
      while slice_count > 2000:
        last_exp = exp
        exp += 1
        slice_count = int(math.ceil(float(self.end_time - self.start_time) / float(pow(10, exp))))
      self.cpu['total_usecs'] = self.end_time - self.start_time
      self.cpu['slice_usecs'] = int(pow(10, last_exp))
      slice_count = int(math.ceil(float(self.end_time - self.start_time) / float(self.cpu['slice_usecs'])))

      # Create the empty time slices for all of the threads
      self.cpu['slices'] = {}
      for thread in self.threads.keys():
        self.cpu['slices'][thread] = {'total': [0.0] * slice_count}
        for name in self.threads[thread].keys():
          self.cpu['slices'][thread][name] = [0.0] * slice_count

      # Go through all of the timeline events recursively and account for the time they consumed
      for timeline_event in self.timeline_events:
        self.ProcessTimelineEvent(timeline_event, None)

      # Go through all of the fractional times and convert the float fractional times to integer usecs
      for thread in self.cpu['slices'].keys():
        del self.cpu['slices'][thread]['total']
        for name in self.cpu['slices'][thread].keys():
          for slice in range(len(self.cpu['slices'][thread][name])):
            self.cpu['slices'][thread][name][slice] =\
              int(self.cpu['slices'][thread][name][slice] * self.cpu['slice_usecs'])

  def ProcessTimelineEvent(self, timeline_event, parent):
    start = timeline_event['s'] - self.start_time
    end = timeline_event['e'] - self.start_time
    if end > start:
      thread = timeline_event['t']
      name = self.event_name_lookup[timeline_event['n']]

      if 'js' in timeline_event:
        script = timeline_event['js']
        s = start / 1000.0
        e = end / 1000.0
        if self.scripts is None:
          self.scripts = {}
        if 'main_thread' not in self.scripts and 'main_thread' in self.cpu:
          self.scripts['main_thread'] = self.cpu['main_thread']
        if thread not in self.scripts:
          self.scripts[thread] = {}
        if script not in self.scripts[thread]:
          self.scripts[thread][script] = {}
        if name not in self.scripts[thread][script]:
          self.scripts[thread][script][name] = []
        self.scripts[thread][script][name].append([s, e])

      slice_usecs = self.cpu['slice_usecs']
      first_slice = int(float(start) / float(slice_usecs))
      last_slice = int(float(end) / float(slice_usecs))
      for slice_number in xrange(first_slice, last_slice + 1):
        slice_start = slice_number * slice_usecs
        slice_end = slice_start + slice_usecs
        used_start = max(slice_start, start)
        used_end = min(slice_end, end)
        slice_elapsed = used_end - used_start
        self.AdjustTimelineSlice(thread, slice_number, name, parent, slice_elapsed)

      # Recursively process any child events
      if 'c' in timeline_event:
        for child in timeline_event['c']:
          self.ProcessTimelineEvent(child, name)

  # Add the time to the given slice and subtract the time from a parent event
  def AdjustTimelineSlice(self, thread, slice_number, name, parent, elapsed):
    try:
      # Don't bother adjusting if both the current event and parent are the same category
      # since they would just cancel each other out.
      if name != parent:
        fraction = min(1.0, float(elapsed) / float(self.cpu['slice_usecs']))
        self.cpu['slices'][thread][name][slice_number] += fraction
        self.cpu['slices'][thread]['total'] += fraction
        if parent is not None and self.cpu['slices'][thread][parent][slice_number] >= fraction:
          self.cpu['slices'][thread][parent][slice_number] -= fraction
          self.cpu['slices'][thread]['total'] -= fraction
        # Make sure we didn't exceed 100% in this slice
        self.cpu['slices'][thread][name][slice_number] = min(1.0, self.cpu['slices'][thread][name][slice_number])

        # make sure we don't exceed 100% for any slot
        if self.cpu['slices'][thread]['total'] > 1.0:
          available = max(0.0, 1.0 - fraction)
          for slice_name in self.cpu['slices'][thread].keys():
            if slice_name != name:
              self.cpu['slices'][thread][slice_name][slice_number] =\
                min(self.cpu['slices'][thread][slice_name][slice_number], available)
              available = max(0.0, available - self.cpu['slices'][thread][slice_name][slice_number])
          self.cpu['slices'][thread]['total'] = min(1.0, max(0.0, 1.0 - available))
    except:
      pass

  ########################################################################################################################
  #   Blink Features
  ########################################################################################################################
  def ProcessFeatureUsageEvent(self, trace_event):
    global BLINK_CSS_FEATURES
    global BLINK_FEATURES
    if 'name' in trace_event and\
            'args' in trace_event and\
            'feature' in trace_event['args'] and\
        (trace_event['name'] == 'FeatureFirstUsed' or trace_event['name'] == 'CSSFeatureFirstUsed'):
      if self.feature_usage is None:
        self.feature_usage = {'Features': {}, 'CSSFeatures': {}}
      if self.feature_usage_start_time is None:
        if self.start_time is not None:
          self.feature_usage_start_time = self.start_time
        else:
          self.feature_usage_start_time = trace_event['ts']
      id = '{0:d}'.format(trace_event['args']['feature'])
      timestamp = float('{0:0.3f}'.format((trace_event['ts'] - self.feature_usage_start_time) / 1000.0))
      if trace_event['name'] == 'FeatureFirstUsed':
        if id in BLINK_FEATURES:
          name = BLINK_FEATURES[id]
        else:
          name = 'Feature_{0}'.format(id)
        if name not in self.feature_usage['Features']:
          self.feature_usage['Features'][name] = timestamp
      elif trace_event['name'] == 'CSSFeatureFirstUsed':
        if id in BLINK_CSS_FEATURES:
          name = BLINK_CSS_FEATURES[id]
        else:
          name = 'CSSFeature_{0}'.format(id)
        if name not in self.feature_usage['CSSFeatures']:
          self.feature_usage['CSSFeatures'][name] = timestamp

  ########################################################################################################################
  #   Netlog
  ########################################################################################################################
  def ProcessNetlogEvent(self, trace_event):
    if 'args' in trace_event and 'id' in trace_event and 'name' in trace_event and 'source_type' in trace_event['args']:
      # Convert the source event id to hex if one exists
      if 'params' in trace_event['args'] and 'source_dependency' in trace_event['args']['params'] and 'id' in trace_event['args']['params']['source_dependency']:
        dependency_id = int(trace_event['args']['params']['source_dependency']['id'])
        trace_event['args']['params']['source_dependency']['id'] = 'x%X' % dependency_id
      if trace_event['args']['source_type'] == 'SOCKET':
        self.ProcessNetlogSocketEvent(trace_event)
      if trace_event['args']['source_type'] == 'HTTP2_SESSION':
        self.ProcessNetlogHTTP2SessionEvent(trace_event)

  def ProcessNetlogSocketEvent(self, s):
    if 'sockets' not in self.netlog:
      self.netlog['sockets'] = {}
    if s['id'] not in self.netlog['sockets']:
      self.netlog['sockets'][s['id']] = {'bytes_in': 0, 'bytes_out': 0}
    if s['name'] == 'SOCKET_BYTES_RECEIVED' and 'params' in s['args'] and 'byte_count' in s['args']['params']:
      self.netlog['sockets'][s['id']]['bytes_in'] += s['args']['params']['byte_count']
      self.netlog['bytes_in'] += s['args']['params']['byte_count']
    if s['name'] == 'SOCKET_BYTES_SENT' and 'params' in s['args'] and 'byte_count' in s['args']['params']:
      self.netlog['sockets'][s['id']]['bytes_out'] += s['args']['params']['byte_count']
      self.netlog['bytes_out'] += s['args']['params']['byte_count']

  def ProcessNetlogHTTP2SessionEvent(self, s):
    if 'params' in s['args'] and 'stream_id' in s['args']['params']:
      if 'http2' not in self.netlog:
        self.netlog['http2'] = {'bytes_in': 0, 'bytes_out': 0}
      if s['id'] not in self.netlog['http2']:
        self.netlog['http2'][s['id']] = {'bytes_in': 0, 'bytes_out': 0, 'streams':{}}
      stream = '{0:d}'.format(s['args']['params']['stream_id'])
      if stream not in self.netlog['http2'][s['id']]['streams']:
        self.netlog['http2'][s['id']]['streams'][stream] = {'start': s['tts'], 'end': s['tts'], 'bytes_in': 0, 'bytes_out': 0}
      if s['tts'] > self.netlog['http2'][s['id']]['streams'][stream]['end']:
        self.netlog['http2'][s['id']]['streams'][stream]['end'] = s['tts']

    if s['name'] == 'HTTP2_SESSION_SEND_HEADERS' and 'params' in s['args']:
      if 'request' not in self.netlog['http2'][s['id']]['streams'][stream]:
        self.netlog['http2'][s['id']]['streams'][stream]['request'] = {}
      if 'headers' in s['args']['params']:
        self.netlog['http2'][s['id']]['streams'][stream]['request']['headers'] = s['args']['params']['headers']
      if 'parent_stream_id' in s['args']['params']:
        self.netlog['http2'][s['id']]['streams'][stream]['request']['parent_stream_id'] = s['args']['params']['parent_stream_id']
      if 'exclusive' in s['args']['params']:
        self.netlog['http2'][s['id']]['streams'][stream]['request']['exclusive'] = s['args']['params']['exclusive']
      if 'priority' in s['args']['params']:
        self.netlog['http2'][s['id']]['streams'][stream]['request']['priority'] = s['args']['params']['priority']

    if s['name'] == 'HTTP2_SESSION_RECV_HEADERS' and 'params' in s['args']:
      if 'first_byte' not in self.netlog['http2'][s['id']]['streams'][stream]:
        self.netlog['http2'][s['id']]['streams'][stream]['first_byte'] = s['tts']
      if 'response' not in self.netlog['http2'][s['id']]['streams'][stream]:
        self.netlog['http2'][s['id']]['streams'][stream]['response'] = {}
      if 'headers' in s['args']['params']:
        self.netlog['http2'][s['id']]['response']['streams'][stream]['headers'] = s['args']['params']['headers']

    if s['name'] == 'HTTP2_SESSION_RECV_DATA' and 'params' in s['args'] and 'size' in s['args']['params']:
      if 'first_byte' not in self.netlog['http2'][s['id']]['streams'][stream]:
        self.netlog['http2'][s['id']]['streams'][stream]['first_byte'] = s['tts']
      self.netlog['http2'][s['id']]['streams'][stream]['bytes_in'] += s['args']['params']['size']
      self.netlog['http2'][s['id']]['bytes_in'] += s['args']['params']['size']


########################################################################################################################
#   Main Entry Point
########################################################################################################################
def main():
  import argparse
  parser = argparse.ArgumentParser(description='Chrome trace parser.',
                                   prog='trace-parser')
  parser.add_argument('-v', '--verbose', action='count',
                      help="Increase verbosity (specify multiple times for more). -vvvv for full debug output.")
  parser.add_argument('-t', '--trace', help="Input trace file.")
  parser.add_argument('-c', '--cpu', help="Output CPU time slices file.")
  parser.add_argument('-j', '--js', help="Output Javascript per-script parse/evaluate/execute timings.")
  parser.add_argument('-u', '--user', help="Output user timing file.")
  parser.add_argument('-f', '--features', help="Output blink feature usage file.")
  parser.add_argument('-n', '--netlog', help="Output netlog details file.")
  parser.add_argument('-a', '--all', action='store_true', default=False,
                      help="Load the whole trace into memory (defaults to parsing incrementally).")
  options = parser.parse_args()

  # Set up logging
  log_level = logging.CRITICAL
  if options.verbose == 1:
    log_level = logging.ERROR
  elif options.verbose == 2:
    log_level = logging.WARNING
  elif options.verbose == 3:
    log_level = logging.INFO
  elif options.verbose >= 4:
    log_level = logging.DEBUG
  logging.basicConfig(level=log_level, format="%(asctime)s.%(msecs)03d - %(message)s", datefmt="%H:%M:%S")

  if not options.trace:
    parser.error("Input trace file is not specified.")

  start = time.time()
  trace = Trace()
  trace.Process(options.trace, options.all)

  if options.user:
    trace.WriteUserTiming(options.user)

  if options.cpu:
    trace.WriteCPUSlices(options.cpu)

  if options.js:
    trace.WriteScriptTimings(options.js)

  if options.features:
    trace.WriteFeatureUsage(options.features)

  if options.netlog:
    trace.WriteNetlog(options.netlog)

  end = time.time()
  elapsed = end - start
  logging.debug("Elapsed Time: {0:0.4f}".format(elapsed))


########################################################################################################################
#   Blink feature names from https://cs.chromium.org/chromium/src/third_party/WebKit/Source/core/frame/UseCounter.h
########################################################################################################################
BLINK_FEATURES = {
  "0": "PageDestruction",
  "3": "PrefixedIndexedDB",
  "4": "WorkerStart",
  "5": "SharedWorkerStart",
  "9": "UnprefixedIndexedDB",
  "10": "OpenWebDatabase",
  "13": "UnprefixedRequestAnimationFrame",
  "14": "PrefixedRequestAnimationFrame",
  "15": "ContentSecurityPolicy",
  "16": "ContentSecurityPolicyReportOnly",
  "18": "PrefixedTransitionEndEvent",
  "19": "UnprefixedTransitionEndEvent",
  "20": "PrefixedAndUnprefixedTransitionEndEvent",
  "21": "AutoFocusAttribute",
  "23": "DataListElement",
  "24": "FormAttribute",
  "25": "IncrementalAttribute",
  "26": "InputTypeColor",
  "27": "InputTypeDate",
  "29": "InputTypeDateTimeFallback",
  "30": "InputTypeDateTimeLocal",
  "31": "InputTypeEmail",
  "32": "InputTypeMonth",
  "33": "InputTypeNumber",
  "34": "InputTypeRange",
  "35": "InputTypeSearch",
  "36": "InputTypeTel",
  "37": "InputTypeTime",
  "38": "InputTypeURL",
  "39": "InputTypeWeek",
  "40": "InputTypeWeekFallback",
  "41": "ListAttribute",
  "42": "MaxAttribute",
  "43": "MinAttribute",
  "44": "PatternAttribute",
  "45": "PlaceholderAttribute",
  "47": "PrefixedDirectoryAttribute",
  "49": "RequiredAttribute",
  "51": "StepAttribute",
  "52": "PageVisits",
  "53": "HTMLMarqueeElement",
  "55": "Reflection",
  "57": "PrefixedStorageInfo",
  "58": "XFrameOptions",
  "59": "XFrameOptionsSameOrigin",
  "60": "XFrameOptionsSameOriginWithBadAncestorChain",
  "61": "DeprecatedFlexboxWebContent",
  "62": "DeprecatedFlexboxChrome",
  "63": "DeprecatedFlexboxChromeExtension",
  "65": "UnprefixedPerformanceTimeline",
  "67": "UnprefixedUserTiming",
  "69": "WindowEvent",
  "70": "ContentSecurityPolicyWithBaseElement",
  "74": "DocumentClear",
  "77": "XMLDocument",
  "78": "XSLProcessingInstruction",
  "79": "XSLTProcessor",
  "80": "SVGSwitchElement",
  "83": "DocumentAll",
  "84": "FormElement",
  "85": "DemotedFormElement",
  "90": "SVGAnimationElement",
  "96": "LineClamp",
  "97": "SubFrameBeforeUnloadRegistered",
  "98": "SubFrameBeforeUnloadFired",
  "102": "ConsoleMarkTimeline",
  "111": "DocumentCreateAttribute",
  "112": "DocumentCreateAttributeNS",
  "113": "DocumentCreateCDATASection",
  "115": "DocumentXMLEncoding",
  "116": "DocumentXMLStandalone",
  "117": "DocumentXMLVersion",
  "123": "NavigatorProductSub",
  "124": "NavigatorVendor",
  "125": "NavigatorVendorSub",
  "128": "PrefixedAnimationEndEvent",
  "129": "UnprefixedAnimationEndEvent",
  "130": "PrefixedAndUnprefixedAnimationEndEvent",
  "131": "PrefixedAnimationStartEvent",
  "132": "UnprefixedAnimationStartEvent",
  "133": "PrefixedAndUnprefixedAnimationStartEvent",
  "134": "PrefixedAnimationIterationEvent",
  "135": "UnprefixedAnimationIterationEvent",
  "136": "PrefixedAndUnprefixedAnimationIterationEvent",
  "137": "EventReturnValue",
  "138": "SVGSVGElement",
  "143": "DOMSubtreeModifiedEvent",
  "144": "DOMNodeInsertedEvent",
  "145": "DOMNodeRemovedEvent",
  "146": "DOMNodeRemovedFromDocumentEvent",
  "147": "DOMNodeInsertedIntoDocumentEvent",
  "148": "DOMCharacterDataModifiedEvent",
  "150": "DocumentAllLegacyCall",
  "152": "HTMLEmbedElementLegacyCall",
  "153": "HTMLObjectElementLegacyCall",
  "155": "GetMatchedCSSRules",
  "160": "AttributeOwnerElement",
  "162": "AttributeSpecified",
  "164": "PrefixedAudioDecodedByteCount",
  "165": "PrefixedVideoDecodedByteCount",
  "166": "PrefixedVideoSupportsFullscreen",
  "167": "PrefixedVideoDisplayingFullscreen",
  "168": "PrefixedVideoEnterFullscreen",
  "169": "PrefixedVideoExitFullscreen",
  "170": "PrefixedVideoEnterFullScreen",
  "171": "PrefixedVideoExitFullScreen",
  "172": "PrefixedVideoDecodedFrameCount",
  "173": "PrefixedVideoDroppedFrameCount",
  "176": "PrefixedElementRequestFullscreen",
  "177": "PrefixedElementRequestFullScreen",
  "178": "BarPropLocationbar",
  "179": "BarPropMenubar",
  "180": "BarPropPersonalbar",
  "181": "BarPropScrollbars",
  "182": "BarPropStatusbar",
  "183": "BarPropToolbar",
  "184": "InputTypeEmailMultiple",
  "185": "InputTypeEmailMaxLength",
  "186": "InputTypeEmailMultipleMaxLength",
  "190": "InputTypeText",
  "191": "InputTypeTextMaxLength",
  "192": "InputTypePassword",
  "193": "InputTypePasswordMaxLength",
  "196": "PrefixedPageVisibility",
  "198": "CSSStyleSheetInsertRuleOptionalArg",
  "200": "DocumentBeforeUnloadRegistered",
  "201": "DocumentBeforeUnloadFired",
  "202": "DocumentUnloadRegistered",
  "203": "DocumentUnloadFired",
  "204": "SVGLocatableNearestViewportElement",
  "205": "SVGLocatableFarthestViewportElement",
  "209": "SVGPointMatrixTransform",
  "211": "DOMFocusInOutEvent",
  "212": "FileGetLastModifiedDate",
  "213": "HTMLElementInnerText",
  "214": "HTMLElementOuterText",
  "215": "ReplaceDocumentViaJavaScriptURL",
  "217": "ElementPrefixedMatchesSelector",
  "219": "CSSStyleSheetRules",
  "220": "CSSStyleSheetAddRule",
  "221": "CSSStyleSheetRemoveRule",
  "222": "InitMessageEvent",
  "233": "PrefixedDevicePixelRatioMediaFeature",
  "234": "PrefixedMaxDevicePixelRatioMediaFeature",
  "235": "PrefixedMinDevicePixelRatioMediaFeature",
  "237": "PrefixedTransform3dMediaFeature",
  "240": "PrefixedStorageQuota",
  "243": "ResetReferrerPolicy",
  "244": "CaseInsensitiveAttrSelectorMatch",
  "246": "FormNameAccessForImageElement",
  "247": "FormNameAccessForPastNamesMap",
  "248": "FormAssociationByParser",
  "250": "SVGSVGElementInDocument",
  "251": "SVGDocumentRootElement",
  "257": "WorkerSubjectToCSP",
  "258": "WorkerAllowedByChildBlockedByScript",
  "260": "DeprecatedWebKitGradient",
  "261": "DeprecatedWebKitLinearGradient",
  "262": "DeprecatedWebKitRepeatingLinearGradient",
  "263": "DeprecatedWebKitRadialGradient",
  "264": "DeprecatedWebKitRepeatingRadialGradient",
  "267": "PrefixedImageSmoothingEnabled",
  "268": "UnprefixedImageSmoothingEnabled",
  "274": "TextAutosizing",
  "276": "HTMLAnchorElementPingAttribute",
  "279": "SVGClassName",
  "281": "HTMLMediaElementSeekToFragmentStart",
  "282": "HTMLMediaElementPauseAtFragmentEnd",
  "283": "PrefixedWindowURL",
  "285": "WindowOrientation",
  "286": "DOMStringListContains",
  "287": "DocumentCaptureEvents",
  "288": "DocumentReleaseEvents",
  "289": "WindowCaptureEvents",
  "290": "WindowReleaseEvents",
  "295": "DocumentXPathCreateExpression",
  "296": "DocumentXPathCreateNSResolver",
  "297": "DocumentXPathEvaluate",
  "298": "AttrGetValue",
  "299": "AttrSetValue",
  "300": "AnimationConstructorKeyframeListEffectObjectTiming",
  "302": "AnimationConstructorKeyframeListEffectNoTiming",
  "303": "AttrSetValueWithElement",
  "304": "PrefixedCancelAnimationFrame",
  "305": "PrefixedCancelRequestAnimationFrame",
  "306": "NamedNodeMapGetNamedItem",
  "307": "NamedNodeMapSetNamedItem",
  "308": "NamedNodeMapRemoveNamedItem",
  "309": "NamedNodeMapItem",
  "310": "NamedNodeMapGetNamedItemNS",
  "311": "NamedNodeMapSetNamedItemNS",
  "312": "NamedNodeMapRemoveNamedItemNS",
  "318": "PrefixedDocumentIsFullscreen",
  "320": "PrefixedDocumentCurrentFullScreenElement",
  "321": "PrefixedDocumentCancelFullScreen",
  "322": "PrefixedDocumentFullscreenEnabled",
  "323": "PrefixedDocumentFullscreenElement",
  "324": "PrefixedDocumentExitFullscreen",
  "325": "SVGForeignObjectElement",
  "327": "SelectionSetPosition",
  "328": "AnimationFinishEvent",
  "329": "SVGSVGElementInXMLDocument",
  "341": "PrefixedPerformanceClearResourceTimings",
  "342": "PrefixedPerformanceSetResourceTimingBufferSize",
  "343": "EventSrcElement",
  "344": "EventCancelBubble",
  "345": "EventPath",
  "347": "NodeIteratorDetach",
  "348": "AttrNodeValue",
  "349": "AttrTextContent",
  "350": "EventGetReturnValueTrue",
  "351": "EventGetReturnValueFalse",
  "352": "EventSetReturnValueTrue",
  "353": "EventSetReturnValueFalse",
  "356": "WindowOffscreenBuffering",
  "357": "WindowDefaultStatus",
  "358": "WindowDefaultstatus",
  "361": "PrefixedTransitionEventConstructor",
  "362": "PrefixedMutationObserverConstructor",
  "363": "PrefixedIDBCursorConstructor",
  "364": "PrefixedIDBDatabaseConstructor",
  "365": "PrefixedIDBFactoryConstructor",
  "366": "PrefixedIDBIndexConstructor",
  "367": "PrefixedIDBKeyRangeConstructor",
  "368": "PrefixedIDBObjectStoreConstructor",
  "369": "PrefixedIDBRequestConstructor",
  "370": "PrefixedIDBTransactionConstructor",
  "371": "NotificationPermission",
  "372": "RangeDetach",
  "386": "PrefixedFileRelativePath",
  "387": "DocumentCaretRangeFromPoint",
  "389": "ElementScrollIntoViewIfNeeded",
  "393": "RangeExpand",
  "396": "HTMLImageElementX",
  "397": "HTMLImageElementY",
  "400": "SelectionBaseNode",
  "401": "SelectionBaseOffset",
  "402": "SelectionExtentNode",
  "403": "SelectionExtentOffset",
  "404": "SelectionType",
  "405": "SelectionModify",
  "406": "SelectionSetBaseAndExtent",
  "407": "SelectionEmpty",
  "409": "VTTCue",
  "410": "VTTCueRender",
  "411": "VTTCueRenderVertical",
  "412": "VTTCueRenderSnapToLinesFalse",
  "413": "VTTCueRenderLineNotAuto",
  "414": "VTTCueRenderPositionNot50",
  "415": "VTTCueRenderSizeNot100",
  "416": "VTTCueRenderAlignNotMiddle",
  "417": "ElementRequestPointerLock",
  "418": "VTTCueRenderRtl",
  "419": "PostMessageFromSecureToInsecure",
  "420": "PostMessageFromInsecureToSecure",
  "421": "DocumentExitPointerLock",
  "422": "DocumentPointerLockElement",
  "424": "PrefixedCursorZoomIn",
  "425": "PrefixedCursorZoomOut",
  "429": "TextEncoderConstructor",
  "430": "TextEncoderEncode",
  "431": "TextDecoderConstructor",
  "432": "TextDecoderDecode",
  "433": "FocusInOutEvent",
  "434": "MouseEventMovementX",
  "435": "MouseEventMovementY",
  "440": "DocumentFonts",
  "441": "MixedContentFormsSubmitted",
  "442": "FormsSubmitted",
  "443": "TextInputEventOnInput",
  "444": "TextInputEventOnTextArea",
  "445": "TextInputEventOnContentEditable",
  "446": "TextInputEventOnNotNode",
  "447": "WebkitBeforeTextInsertedOnInput",
  "448": "WebkitBeforeTextInsertedOnTextArea",
  "449": "WebkitBeforeTextInsertedOnContentEditable",
  "450": "WebkitBeforeTextInsertedOnNotNode",
  "451": "WebkitEditableContentChangedOnInput",
  "452": "WebkitEditableContentChangedOnTextArea",
  "453": "WebkitEditableContentChangedOnContentEditable",
  "454": "WebkitEditableContentChangedOnNotNode",
  "455": "HTMLImports",
  "456": "ElementCreateShadowRoot",
  "457": "DocumentRegisterElement",
  "458": "EditingAppleInterchangeNewline",
  "459": "EditingAppleConvertedSpace",
  "460": "EditingApplePasteAsQuotation",
  "461": "EditingAppleStyleSpanClass",
  "462": "EditingAppleTabSpanClass",
  "463": "HTMLImportsAsyncAttribute",
  "465": "XMLHttpRequestSynchronous",
  "466": "CSSSelectorPseudoUnresolved",
  "467": "CSSSelectorPseudoShadow",
  "468": "CSSSelectorPseudoContent",
  "469": "CSSSelectorPseudoHost",
  "470": "CSSSelectorPseudoHostContext",
  "471": "CSSDeepCombinator",
  "473": "UseAsm",
  "475": "DOMWindowOpen",
  "476": "DOMWindowOpenFeatures",
  "478": "MediaStreamTrackGetSources",
  "479": "AspectRatioFlexItem",
  "480": "DetailsElement",
  "481": "DialogElement",
  "482": "MapElement",
  "483": "MeterElement",
  "484": "ProgressElement",
  "490": "PrefixedHTMLElementDropzone",
  "491": "WheelEventWheelDeltaX",
  "492": "WheelEventWheelDeltaY",
  "493": "WheelEventWheelDelta",
  "494": "SendBeacon",
  "495": "SendBeaconQuotaExceeded",
  "501": "SVGSMILElementInDocument",
  "502": "MouseEventOffsetX",
  "503": "MouseEventOffsetY",
  "504": "MouseEventX",
  "505": "MouseEventY",
  "506": "MouseEventFromElement",
  "507": "MouseEventToElement",
  "508": "RequestFileSystem",
  "509": "RequestFileSystemWorker",
  "510": "RequestFileSystemSyncWorker",
  "519": "SVGStyleElementTitle",
  "520": "PictureSourceSrc",
  "521": "Picture",
  "522": "Sizes",
  "523": "SrcsetXDescriptor",
  "524": "SrcsetWDescriptor",
  "525": "SelectionContainsNode",
  "529": "XMLExternalResourceLoad",
  "530": "MixedContentPrivateHostnameInPublicHostname",
  "531": "LegacyProtocolEmbeddedAsSubresource",
  "532": "RequestedSubresourceWithEmbeddedCredentials",
  "533": "NotificationCreated",
  "534": "NotificationClosed",
  "535": "NotificationPermissionRequested",
  "538": "ConsoleTimeline",
  "539": "ConsoleTimelineEnd",
  "540": "SRIElementWithMatchingIntegrityAttribute",
  "541": "SRIElementWithNonMatchingIntegrityAttribute",
  "542": "SRIElementWithUnparsableIntegrityAttribute",
  "545": "V8Animation_StartTime_AttributeGetter",
  "546": "V8Animation_StartTime_AttributeSetter",
  "547": "V8Animation_CurrentTime_AttributeGetter",
  "548": "V8Animation_CurrentTime_AttributeSetter",
  "549": "V8Animation_PlaybackRate_AttributeGetter",
  "550": "V8Animation_PlaybackRate_AttributeSetter",
  "551": "V8Animation_PlayState_AttributeGetter",
  "552": "V8Animation_Finish_Method",
  "553": "V8Animation_Play_Method",
  "554": "V8Animation_Pause_Method",
  "555": "V8Animation_Reverse_Method",
  "556": "BreakIterator",
  "557": "ScreenOrientationAngle",
  "558": "ScreenOrientationType",
  "559": "ScreenOrientationLock",
  "560": "ScreenOrientationUnlock",
  "561": "GeolocationSecureOrigin",
  "562": "GeolocationInsecureOrigin",
  "563": "NotificationSecureOrigin",
  "564": "NotificationInsecureOrigin",
  "565": "NotificationShowEvent",
  "569": "SVGTransformListConsolidate",
  "570": "SVGAnimatedTransformListBaseVal",
  "571": "QuotedAnimationName",
  "572": "QuotedKeyframesRule",
  "573": "SrcsetDroppedCandidate",
  "574": "WindowPostMessage",
  "575": "WindowPostMessageWithLegacyTargetOriginArgument",
  "576": "RenderRuby",
  "578": "ScriptElementWithInvalidTypeHasSrc",
  "581": "XMLHttpRequestSynchronousInNonWorkerOutsideBeforeUnload",
  "582": "CSSSelectorPseudoScrollbar",
  "583": "CSSSelectorPseudoScrollbarButton",
  "584": "CSSSelectorPseudoScrollbarThumb",
  "585": "CSSSelectorPseudoScrollbarTrack",
  "586": "CSSSelectorPseudoScrollbarTrackPiece",
  "587": "LangAttribute",
  "588": "LangAttributeOnHTML",
  "589": "LangAttributeOnBody",
  "590": "LangAttributeDoesNotMatchToUILocale",
  "591": "InputTypeSubmit",
  "592": "InputTypeSubmitWithValue",
  "593": "SetReferrerPolicy",
  "595": "MouseEventWhich",
  "598": "UIEventWhich",
  "599": "TextWholeText",
  "603": "NotificationCloseEvent",
  "606": "StyleMedia",
  "607": "StyleMediaType",
  "608": "StyleMediaMatchMedium",
  "609": "MixedContentPresent",
  "610": "MixedContentBlockable",
  "611": "MixedContentAudio",
  "612": "MixedContentDownload",
  "613": "MixedContentFavicon",
  "614": "MixedContentImage",
  "615": "MixedContentInternal",
  "616": "MixedContentPlugin",
  "617": "MixedContentPrefetch",
  "618": "MixedContentVideo",
  "620": "AudioListenerDopplerFactor",
  "621": "AudioListenerSpeedOfSound",
  "622": "AudioListenerSetVelocity",
  "628": "CSSSelectorPseudoFullScreenAncestor",
  "629": "CSSSelectorPseudoFullScreen",
  "630": "WebKitCSSMatrix",
  "631": "AudioContextCreateAnalyser",
  "632": "AudioContextCreateBiquadFilter",
  "633": "AudioContextCreateBufferSource",
  "634": "AudioContextCreateChannelMerger",
  "635": "AudioContextCreateChannelSplitter",
  "636": "AudioContextCreateConvolver",
  "637": "AudioContextCreateDelay",
  "638": "AudioContextCreateDynamicsCompressor",
  "639": "AudioContextCreateGain",
  "640": "AudioContextCreateMediaElementSource",
  "641": "AudioContextCreateMediaStreamDestination",
  "642": "AudioContextCreateMediaStreamSource",
  "643": "AudioContextCreateOscillator",
  "645": "AudioContextCreatePeriodicWave",
  "646": "AudioContextCreateScriptProcessor",
  "647": "AudioContextCreateStereoPanner",
  "648": "AudioContextCreateWaveShaper",
  "649": "AudioContextDecodeAudioData",
  "650": "AudioContextResume",
  "651": "AudioContextSuspend",
  "652": "AudioContext",
  "653": "OfflineAudioContext",
  "654": "PrefixedAudioContext",
  "655": "PrefixedOfflineAudioContext",
  "661": "MixedContentInNonHTTPSFrameThatRestrictsMixedContent",
  "662": "MixedContentInSecureFrameThatDoesNotRestrictMixedContent",
  "663": "MixedContentWebSocket",
  "664": "SyntheticKeyframesInCompositedCSSAnimation",
  "665": "MixedContentFormPresent",
  "666": "GetUserMediaInsecureOrigin",
  "667": "GetUserMediaSecureOrigin",
  "668": "DeviceMotionInsecureOrigin",
  "669": "DeviceMotionSecureOrigin",
  "670": "DeviceOrientationInsecureOrigin",
  "671": "DeviceOrientationSecureOrigin",
  "672": "SandboxViaIFrame",
  "673": "SandboxViaCSP",
  "674": "BlockedSniffingImageToScript",
  "675": "Fetch",
  "676": "FetchBodyStream",
  "677": "XMLHttpRequestAsynchronous",
  "679": "WhiteSpacePreFromXMLSpace",
  "680": "WhiteSpaceNowrapFromXMLSpace",
  "685": "SVGSVGElementForceRedraw",
  "686": "SVGSVGElementSuspendRedraw",
  "687": "SVGSVGElementUnsuspendRedraw",
  "688": "SVGSVGElementUnsuspendRedrawAll",
  "689": "AudioContextClose",
  "691": "CSSZoomNotEqualToOne",
  "694": "ClientRectListItem",
  "695": "WindowClientInformation",
  "696": "WindowFind",
  "697": "WindowScreenLeft",
  "698": "WindowScreenTop",
  "699": "V8Animation_Cancel_Method",
  "700": "V8Animation_Onfinish_AttributeGetter",
  "701": "V8Animation_Onfinish_AttributeSetter",
  "707": "V8Window_WebKitAnimationEvent_ConstructorGetter",
  "710": "CryptoGetRandomValues",
  "711": "SubtleCryptoEncrypt",
  "712": "SubtleCryptoDecrypt",
  "713": "SubtleCryptoSign",
  "714": "SubtleCryptoVerify",
  "715": "SubtleCryptoDigest",
  "716": "SubtleCryptoGenerateKey",
  "717": "SubtleCryptoImportKey",
  "718": "SubtleCryptoExportKey",
  "719": "SubtleCryptoDeriveBits",
  "720": "SubtleCryptoDeriveKey",
  "721": "SubtleCryptoWrapKey",
  "722": "SubtleCryptoUnwrapKey",
  "723": "CryptoAlgorithmAesCbc",
  "724": "CryptoAlgorithmHmac",
  "725": "CryptoAlgorithmRsaSsaPkcs1v1_5",
  "726": "CryptoAlgorithmSha1",
  "727": "CryptoAlgorithmSha256",
  "728": "CryptoAlgorithmSha384",
  "729": "CryptoAlgorithmSha512",
  "730": "CryptoAlgorithmAesGcm",
  "731": "CryptoAlgorithmRsaOaep",
  "732": "CryptoAlgorithmAesCtr",
  "733": "CryptoAlgorithmAesKw",
  "734": "CryptoAlgorithmRsaPss",
  "735": "CryptoAlgorithmEcdsa",
  "736": "CryptoAlgorithmEcdh",
  "737": "CryptoAlgorithmHkdf",
  "738": "CryptoAlgorithmPbkdf2",
  "739": "DocumentSetDomain",
  "740": "UpgradeInsecureRequestsEnabled",
  "741": "UpgradeInsecureRequestsUpgradedRequest",
  "742": "DocumentDesignMode",
  "743": "GlobalCacheStorage",
  "744": "NetInfo",
  "745": "BackgroundSync",
  "748": "LegacyConst",
  "750": "V8Permissions_Query_Method",
  "754": "V8HTMLInputElement_Autocapitalize_AttributeGetter",
  "755": "V8HTMLInputElement_Autocapitalize_AttributeSetter",
  "756": "V8HTMLTextAreaElement_Autocapitalize_AttributeGetter",
  "757": "V8HTMLTextAreaElement_Autocapitalize_AttributeSetter",
  "758": "SVGHrefBaseVal",
  "759": "SVGHrefAnimVal",
  "760": "V8CSSRuleList_Item_Method",
  "761": "V8MediaList_Item_Method",
  "762": "V8StyleSheetList_Item_Method",
  "763": "StyleSheetListAnonymousNamedGetter",
  "764": "AutocapitalizeAttribute",
  "765": "FullscreenSecureOrigin",
  "766": "FullscreenInsecureOrigin",
  "767": "DialogInSandboxedContext",
  "768": "SVGSMILAnimationInImageRegardlessOfCache",
  "770": "EncryptedMediaSecureOrigin",
  "771": "EncryptedMediaInsecureOrigin",
  "772": "PerformanceFrameTiming",
  "773": "V8Element_Animate_Method",
  "778": "V8SVGSVGElement_GetElementById_Method",
  "779": "ElementCreateShadowRootMultiple",
  "780": "V8MessageChannel_Constructor",
  "781": "V8MessagePort_PostMessage_Method",
  "782": "V8MessagePort_Start_Method",
  "783": "V8MessagePort_Close_Method",
  "784": "MessagePortsTransferred",
  "785": "CSSKeyframesRuleAnonymousIndexedGetter",
  "786": "V8Screen_AvailLeft_AttributeGetter",
  "787": "V8Screen_AvailTop_AttributeGetter",
  "791": "V8SVGFEConvolveMatrixElement_PreserveAlpha_AttributeGetter",
  "798": "V8SVGStyleElement_Disabled_AttributeGetter",
  "799": "V8SVGStyleElement_Disabled_AttributeSetter",
  "801": "InputTypeFileSecureOrigin",
  "802": "InputTypeFileInsecureOrigin",
  "804": "ElementAttachShadow",
  "806": "V8SecurityPolicyViolationEvent_DocumentURI_AttributeGetter",
  "807": "V8SecurityPolicyViolationEvent_BlockedURI_AttributeGetter",
  "808": "V8SecurityPolicyViolationEvent_StatusCode_AttributeGetter",
  "809": "HTMLLinkElementDisabled",
  "810": "V8HTMLLinkElement_Disabled_AttributeGetter",
  "811": "V8HTMLLinkElement_Disabled_AttributeSetter",
  "812": "V8HTMLStyleElement_Disabled_AttributeGetter",
  "813": "V8HTMLStyleElement_Disabled_AttributeSetter",
  "816": "V8DOMError_Constructor",
  "817": "V8DOMError_Name_AttributeGetter",
  "818": "V8DOMError_Message_AttributeGetter",
  "823": "V8Location_AncestorOrigins_AttributeGetter",
  "824": "V8IDBDatabase_ObjectStoreNames_AttributeGetter",
  "825": "V8IDBObjectStore_IndexNames_AttributeGetter",
  "826": "V8IDBTransaction_ObjectStoreNames_AttributeGetter",
  "830": "TextInputFired",
  "831": "V8TextEvent_Data_AttributeGetter",
  "832": "V8TextEvent_InitTextEvent_Method",
  "833": "V8SVGSVGElement_UseCurrentView_AttributeGetter",
  "834": "V8SVGSVGElement_CurrentView_AttributeGetter",
  "835": "ClientHintsDPR",
  "836": "ClientHintsResourceWidth",
  "837": "ClientHintsViewportWidth",
  "838": "SRIElementIntegrityAttributeButIneligible",
  "839": "FormDataAppendFile",
  "840": "FormDataAppendFileWithFilename",
  "841": "FormDataAppendBlob",
  "842": "FormDataAppendBlobWithFilename",
  "843": "FormDataAppendNull",
  "844": "HTMLDocumentCreateAttributeNameNotLowercase",
  "845": "NonHTMLElementSetAttributeNodeFromHTMLDocumentNameNotLowercase",
  "846": "DOMStringList_Item_AttributeGetter_IndexedDB",
  "847": "DOMStringList_Item_AttributeGetter_Location",
  "848": "DOMStringList_Contains_Method_IndexedDB",
  "849": "DOMStringList_Contains_Method_Location",
  "850": "NavigatorVibrate",
  "851": "NavigatorVibrateSubFrame",
  "853": "V8XPathEvaluator_Constructor",
  "854": "V8XPathEvaluator_CreateExpression_Method",
  "855": "V8XPathEvaluator_CreateNSResolver_Method",
  "856": "V8XPathEvaluator_Evaluate_Method",
  "857": "RequestMIDIAccess",
  "858": "V8MouseEvent_LayerX_AttributeGetter",
  "859": "V8MouseEvent_LayerY_AttributeGetter",
  "860": "InnerTextWithShadowTree",
  "861": "SelectionToStringWithShadowTree",
  "862": "WindowFindWithShadowTree",
  "863": "V8CompositionEvent_InitCompositionEvent_Method",
  "864": "V8CustomEvent_InitCustomEvent_Method",
  "865": "V8DeviceMotionEvent_InitDeviceMotionEvent_Method",
  "866": "V8DeviceOrientationEvent_InitDeviceOrientationEvent_Method",
  "867": "V8Event_InitEvent_Method",
  "868": "V8KeyboardEvent_InitKeyboardEvent_Method",
  "869": "V8MouseEvent_InitMouseEvent_Method",
  "870": "V8MutationEvent_InitMutationEvent_Method",
  "871": "V8StorageEvent_InitStorageEvent_Method",
  "872": "V8TouchEvent_InitTouchEvent_Method",
  "873": "V8UIEvent_InitUIEvent_Method",
  "874": "V8Document_CreateTouch_Method",
  "876": "RequestFileSystemNonWebbyOrigin",
  "879": "V8MemoryInfo_TotalJSHeapSize_AttributeGetter",
  "880": "V8MemoryInfo_UsedJSHeapSize_AttributeGetter",
  "881": "V8MemoryInfo_JSHeapSizeLimit_AttributeGetter",
  "882": "V8Performance_Timing_AttributeGetter",
  "883": "V8Performance_Navigation_AttributeGetter",
  "884": "V8Performance_Memory_AttributeGetter",
  "885": "V8SharedWorker_WorkerStart_AttributeGetter",
  "886": "HTMLKeygenElement",
  "892": "HTMLMediaElementPreloadNone",
  "893": "HTMLMediaElementPreloadMetadata",
  "894": "HTMLMediaElementPreloadAuto",
  "895": "HTMLMediaElementPreloadDefault",
  "896": "MixedContentBlockableAllowed",
  "897": "PseudoBeforeAfterForInputElement",
  "898": "V8Permissions_Revoke_Method",
  "899": "LinkRelDnsPrefetch",
  "900": "LinkRelPreconnect",
  "901": "LinkRelPreload",
  "902": "LinkHeaderDnsPrefetch",
  "903": "LinkHeaderPreconnect",
  "904": "ClientHintsMetaAcceptCH",
  "905": "HTMLElementDeprecatedWidth",
  "906": "ClientHintsContentDPR",
  "907": "ElementAttachShadowOpen",
  "908": "ElementAttachShadowClosed",
  "909": "AudioParamSetValueAtTime",
  "910": "AudioParamLinearRampToValueAtTime",
  "911": "AudioParamExponentialRampToValueAtTime",
  "912": "AudioParamSetTargetAtTime",
  "913": "AudioParamSetValueCurveAtTime",
  "914": "AudioParamCancelScheduledValues",
  "915": "V8Permissions_Request_Method",
  "917": "LinkRelPrefetch",
  "918": "LinkRelPrerender",
  "919": "LinkRelNext",
  "920": "PrefixedPerformanceResourceTimingBufferFull",
  "921": "CSSValuePrefixedMinContent",
  "922": "CSSValuePrefixedMaxContent",
  "923": "CSSValuePrefixedFitContent",
  "924": "CSSValuePrefixedFillAvailable",
  "926": "PresentationDefaultRequest",
  "927": "PresentationAvailabilityChangeEventListener",
  "928": "PresentationRequestConstructor",
  "929": "PresentationRequestStart",
  "930": "PresentationRequestReconnect",
  "931": "PresentationRequestGetAvailability",
  "932": "PresentationRequestConnectionAvailableEventListener",
  "933": "PresentationConnectionTerminate",
  "934": "PresentationConnectionSend",
  "936": "PresentationConnectionMessageEventListener",
  "937": "CSSAnimationsStackedNeutralKeyframe",
  "938": "ReadingCheckedInClickHandler",
  "939": "FlexboxIntrinsicSizeAlgorithmIsDifferent",
  "940": "HTMLImportsHasStyleSheets",
  "944": "ClipPathOfPositionedElement",
  "945": "ClipCssOfPositionedElement",
  "946": "NetInfoType",
  "947": "NetInfoDownlinkMax",
  "948": "NetInfoOnChange",
  "949": "NetInfoOnTypeChange",
  "950": "V8Window_Alert_Method",
  "951": "V8Window_Confirm_Method",
  "952": "V8Window_Prompt_Method",
  "953": "V8Window_Print_Method",
  "954": "V8Window_RequestIdleCallback_Method",
  "955": "FlexboxPercentagePaddingVertical",
  "956": "FlexboxPercentageMarginVertical",
  "957": "BackspaceNavigatedBack",
  "958": "BackspaceNavigatedBackAfterFormInteraction",
  "959": "CSPSourceWildcardWouldMatchExactHost",
  "960": "CredentialManagerGet",
  "961": "CredentialManagerGetWithUI",
  "962": "CredentialManagerGetWithoutUI",
  "963": "CredentialManagerStore",
  "964": "CredentialManagerRequireUserMediation",
  "966": "BlockableMixedContentInSubframeBlocked",
  "967": "AddEventListenerThirdArgumentIsObject",
  "968": "RemoveEventListenerThirdArgumentIsObject",
  "969": "CSSAtRuleCharset",
  "970": "CSSAtRuleFontFace",
  "971": "CSSAtRuleImport",
  "972": "CSSAtRuleKeyframes",
  "973": "CSSAtRuleMedia",
  "974": "CSSAtRuleNamespace",
  "975": "CSSAtRulePage",
  "976": "CSSAtRuleSupports",
  "977": "CSSAtRuleViewport",
  "978": "CSSAtRuleWebkitKeyframes",
  "979": "V8HTMLFieldSetElement_Elements_AttributeGetter",
  "980": "HTMLMediaElementPreloadForcedNone",
  "981": "ExternalAddSearchProvider",
  "982": "ExternalIsSearchProviderInstalled",
  "983": "V8Permissions_RequestAll_Method",
  "987": "DeviceOrientationAbsoluteInsecureOrigin",
  "988": "DeviceOrientationAbsoluteSecureOrigin",
  "989": "FontFaceConstructor",
  "990": "ServiceWorkerControlledPage",
  "993": "MeterElementWithMeterAppearance",
  "994": "MeterElementWithNoneAppearance",
  "997": "SelectionAnchorNode",
  "998": "SelectionAnchorOffset",
  "999": "SelectionFocusNode",
  "1000": "SelectionFocusOffset",
  "1001": "SelectionIsCollapsed",
  "1002": "SelectionRangeCount",
  "1003": "SelectionGetRangeAt",
  "1004": "SelectionAddRange",
  "1005": "SelectionRemoveAllRanges",
  "1006": "SelectionCollapse",
  "1007": "SelectionCollapseToStart",
  "1008": "SelectionCollapseToEnd",
  "1009": "SelectionExtend",
  "1010": "SelectionSelectAllChildren",
  "1011": "SelectionDeleteDromDocument",
  "1012": "SelectionDOMString",
  "1013": "InputTypeRangeVerticalAppearance",
  "1014": "CSSFilterReference",
  "1015": "CSSFilterGrayscale",
  "1016": "CSSFilterSepia",
  "1017": "CSSFilterSaturate",
  "1018": "CSSFilterHueRotate",
  "1019": "CSSFilterInvert",
  "1020": "CSSFilterOpacity",
  "1021": "CSSFilterBrightness",
  "1022": "CSSFilterContrast",
  "1023": "CSSFilterBlur",
  "1024": "CSSFilterDropShadow",
  "1025": "BackgroundSyncRegister",
  "1027": "ExecCommandOnInputOrTextarea",
  "1028": "V8History_ScrollRestoration_AttributeGetter",
  "1029": "V8History_ScrollRestoration_AttributeSetter",
  "1030": "SVG1DOMFilter",
  "1031": "OfflineAudioContextStartRendering",
  "1032": "OfflineAudioContextSuspend",
  "1033": "OfflineAudioContextResume",
  "1034": "AttrCloneNode",
  "1035": "SVG1DOMPaintServer",
  "1036": "SVGSVGElementFragmentSVGView",
  "1037": "SVGSVGElementFragmentSVGViewElement",
  "1038": "PresentationConnectionClose",
  "1039": "SVG1DOMShape",
  "1040": "SVG1DOMText",
  "1041": "RTCPeerConnectionConstructorConstraints",
  "1042": "RTCPeerConnectionConstructorCompliant",
  "1044": "RTCPeerConnectionCreateOfferLegacyFailureCallback",
  "1045": "RTCPeerConnectionCreateOfferLegacyConstraints",
  "1046": "RTCPeerConnectionCreateOfferLegacyOfferOptions",
  "1047": "RTCPeerConnectionCreateOfferLegacyCompliant",
  "1049": "RTCPeerConnectionCreateAnswerLegacyFailureCallback",
  "1050": "RTCPeerConnectionCreateAnswerLegacyConstraints",
  "1051": "RTCPeerConnectionCreateAnswerLegacyCompliant",
  "1052": "RTCPeerConnectionSetLocalDescriptionLegacyNoSuccessCallback",
  "1053": "RTCPeerConnectionSetLocalDescriptionLegacyNoFailureCallback",
  "1054": "RTCPeerConnectionSetLocalDescriptionLegacyCompliant",
  "1055": "RTCPeerConnectionSetRemoteDescriptionLegacyNoSuccessCallback",
  "1056": "RTCPeerConnectionSetRemoteDescriptionLegacyNoFailureCallback",
  "1057": "RTCPeerConnectionSetRemoteDescriptionLegacyCompliant",
  "1058": "RTCPeerConnectionGetStatsLegacyNonCompliant",
  "1059": "NodeFilterIsFunction",
  "1060": "NodeFilterIsObject",
  "1062": "CSSSelectorInternalPseudoListBox",
  "1063": "CSSSelectorInternalMediaControlsCastButton",
  "1064": "CSSSelectorInternalMediaControlsOverlayCastButton",
  "1065": "CSSSelectorInternalPseudoSpatialNavigationFocus",
  "1066": "SameOriginTextScript",
  "1067": "SameOriginApplicationScript",
  "1068": "SameOriginOtherScript",
  "1069": "CrossOriginTextScript",
  "1070": "CrossOriginApplicationScript",
  "1071": "CrossOriginOtherScript",
  "1072": "SVG1DOMSVGTests",
  "1073": "V8SVGViewElement_ViewTarget_AttributeGetter",
  "1074": "DisableRemotePlaybackAttribute",
  "1075": "V8SloppyMode",
  "1076": "V8StrictMode",
  "1077": "V8StrongMode",
  "1078": "AudioNodeConnectToAudioNode",
  "1079": "AudioNodeConnectToAudioParam",
  "1080": "AudioNodeDisconnectFromAudioNode",
  "1081": "AudioNodeDisconnectFromAudioParam",
  "1082": "V8CSSFontFaceRule_Style_AttributeGetter",
  "1083": "SelectionCollapseNull",
  "1084": "SelectionSetBaseAndExtentNull",
  "1085": "V8SVGSVGElement_CreateSVGNumber_Method",
  "1086": "V8SVGSVGElement_CreateSVGLength_Method",
  "1087": "V8SVGSVGElement_CreateSVGAngle_Method",
  "1088": "V8SVGSVGElement_CreateSVGPoint_Method",
  "1089": "V8SVGSVGElement_CreateSVGMatrix_Method",
  "1090": "V8SVGSVGElement_CreateSVGRect_Method",
  "1091": "V8SVGSVGElement_CreateSVGTransform_Method",
  "1092": "V8SVGSVGElement_CreateSVGTransformFromMatrix_Method",
  "1093": "FormNameAccessForNonDescendantImageElement",
  "1095": "V8SVGSVGElement_Viewport_AttributeGetter",
  "1096": "V8RegExpPrototypeStickyGetter",
  "1097": "V8RegExpPrototypeToString",
  "1098": "V8InputDeviceCapabilities_FiresTouchEvents_AttributeGetter",
  "1099": "DataElement",
  "1100": "TimeElement",
  "1101": "SVG1DOMUriReference",
  "1102": "SVG1DOMZoomAndPan",
  "1103": "V8SVGGraphicsElement_Transform_AttributeGetter",
  "1104": "MenuItemElement",
  "1105": "MenuItemCloseTag",
  "1106": "SVG1DOMMarkerElement",
  "1107": "SVG1DOMUseElement",
  "1108": "SVG1DOMMaskElement",
  "1109": "V8SVGAElement_Target_AttributeGetter",
  "1110": "V8SVGClipPathElement_ClipPathUnits_AttributeGetter",
  "1111": "SVG1DOMFitToViewBox",
  "1112": "SVG1DOMCursorElement",
  "1113": "V8SVGPathElement_PathLength_AttributeGetter",
  "1114": "SVG1DOMSVGElement",
  "1115": "SVG1DOMImageElement",
  "1116": "SVG1DOMForeignObjectElement",
  "1117": "AudioContextCreateIIRFilter",
  "1118": "CSSSelectorPseudoSlotted",
  "1119": "MediaDevicesEnumerateDevices",
  "1120": "NonSecureSharedWorkerAccessedFromSecureContext",
  "1121": "SecureSharedWorkerAccessedFromNonSecureContext",
  "1123": "EventComposedPath",
  "1124": "LinkHeaderPreload",
  "1125": "MouseWheelEvent",
  "1126": "WheelEvent",
  "1127": "MouseWheelAndWheelEvent",
  "1128": "BodyScrollsInAdditionToViewport",
  "1129": "DocumentDesignModeEnabeld",
  "1130": "ContentEditableTrue",
  "1131": "ContentEditableTrueOnHTML",
  "1132": "ContentEditablePlainTextOnly",
  "1133": "V8RegExpPrototypeUnicodeGetter",
  "1134": "V8IntlV8Parse",
  "1135": "V8IntlPattern",
  "1136": "V8IntlResolved",
  "1137": "V8PromiseChain",
  "1138": "V8PromiseAccept",
  "1139": "V8PromiseDefer",
  "1140": "EventComposed",
  "1141": "GeolocationInsecureOriginIframe",
  "1142": "GeolocationSecureOriginIframe",
  "1143": "RequestMIDIAccessIframe",
  "1144": "GetUserMediaInsecureOriginIframe",
  "1145": "GetUserMediaSecureOriginIframe",
  "1146": "ElementRequestPointerLockIframe",
  "1147": "NotificationAPIInsecureOriginIframe",
  "1148": "NotificationAPISecureOriginIframe",
  "1149": "WebSocket",
  "1150": "MediaStreamConstraintsNameValue",
  "1151": "MediaStreamConstraintsFromDictionary",
  "1152": "MediaStreamConstraintsConformant",
  "1153": "CSSSelectorIndirectAdjacent",
  "1156": "CreateImageBitmap",
  "1157": "PresentationConnectionConnectEventListener",
  "1158": "PresentationConnectionCloseEventListener",
  "1159": "PresentationConnectionTerminateEventListener",
  "1160": "DocumentCreateEventFontFaceSetLoadEvent",
  "1161": "DocumentCreateEventMediaQueryListEvent",
  "1162": "DocumentCreateEventAnimationEvent",
  "1164": "DocumentCreateEventApplicationCacheErrorEvent",
  "1166": "DocumentCreateEventBeforeUnloadEvent",
  "1167": "DocumentCreateEventClipboardEvent",
  "1168": "DocumentCreateEventCompositionEvent",
  "1169": "DocumentCreateEventDragEvent",
  "1170": "DocumentCreateEventErrorEvent",
  "1171": "DocumentCreateEventFocusEvent",
  "1172": "DocumentCreateEventHashChangeEvent",
  "1173": "DocumentCreateEventMutationEvent",
  "1174": "DocumentCreateEventPageTransitionEvent",
  "1176": "DocumentCreateEventPopStateEvent",
  "1177": "DocumentCreateEventProgressEvent",
  "1178": "DocumentCreateEventPromiseRejectionEvent",
  "1180": "DocumentCreateEventResourceProgressEvent",
  "1181": "DocumentCreateEventSecurityPolicyViolationEvent",
  "1182": "DocumentCreateEventTextEvent",
  "1183": "DocumentCreateEventTransitionEvent",
  "1184": "DocumentCreateEventWheelEvent",
  "1186": "DocumentCreateEventTrackEvent",
  "1187": "DocumentCreateEventWebKitAnimationEvent",
  "1188": "DocumentCreateEventMutationEvents",
  "1189": "DocumentCreateEventOrientationEvent",
  "1190": "DocumentCreateEventSVGEvents",
  "1191": "DocumentCreateEventWebKitTransitionEvent",
  "1192": "DocumentCreateEventBeforeInstallPromptEvent",
  "1193": "DocumentCreateEventSyncEvent",
  "1195": "DocumentCreateEventDeviceMotionEvent",
  "1196": "DocumentCreateEventDeviceOrientationEvent",
  "1197": "DocumentCreateEventMediaEncryptedEvent",
  "1198": "DocumentCreateEventMediaKeyMessageEvent",
  "1199": "DocumentCreateEventGamepadEvent",
  "1201": "DocumentCreateEventIDBVersionChangeEvent",
  "1202": "DocumentCreateEventBlobEvent",
  "1203": "DocumentCreateEventMediaStreamEvent",
  "1204": "DocumentCreateEventMediaStreamTrackEvent",
  "1205": "DocumentCreateEventRTCDTMFToneChangeEvent",
  "1206": "DocumentCreateEventRTCDataChannelEvent",
  "1207": "DocumentCreateEventRTCIceCandidateEvent",
  "1209": "DocumentCreateEventNotificationEvent",
  "1210": "DocumentCreateEventPresentationConnectionAvailableEvent",
  "1211": "DocumentCreateEventPresentationConnectionCloseEvent",
  "1212": "DocumentCreateEventPushEvent",
  "1213": "DocumentCreateEventExtendableEvent",
  "1214": "DocumentCreateEventExtendableMessageEvent",
  "1215": "DocumentCreateEventFetchEvent",
  "1217": "DocumentCreateEventServiceWorkerMessageEvent",
  "1218": "DocumentCreateEventSpeechRecognitionError",
  "1219": "DocumentCreateEventSpeechRecognitionEvent",
  "1220": "DocumentCreateEventSpeechSynthesisEvent",
  "1221": "DocumentCreateEventStorageEvent",
  "1222": "DocumentCreateEventAudioProcessingEvent",
  "1223": "DocumentCreateEventOfflineAudioCompletionEvent",
  "1224": "DocumentCreateEventWebGLContextEvent",
  "1225": "DocumentCreateEventMIDIConnectionEvent",
  "1226": "DocumentCreateEventMIDIMessageEvent",
  "1227": "DocumentCreateEventCloseEvent",
  "1228": "DocumentCreateEventKeyboardEvents",
  "1229": "HTMLMediaElement",
  "1230": "HTMLMediaElementInDocument",
  "1231": "HTMLMediaElementControlsAttribute",
  "1233": "V8Animation_Oncancel_AttributeGetter",
  "1234": "V8Animation_Oncancel_AttributeSetter",
  "1235": "V8HTMLCommentInExternalScript",
  "1236": "V8HTMLComment",
  "1237": "V8SloppyModeBlockScopedFunctionRedefinition",
  "1238": "V8ForInInitializer",
  "1239": "V8Animation_Id_AttributeGetter",
  "1240": "V8Animation_Id_AttributeSetter",
  "1243": "WebAnimationHyphenatedProperty",
  "1244": "FormControlsCollectionReturnsRadioNodeListForFieldSet",
  "1245": "ApplicationCacheManifestSelectInsecureOrigin",
  "1246": "ApplicationCacheManifestSelectSecureOrigin",
  "1247": "ApplicationCacheAPIInsecureOrigin",
  "1248": "ApplicationCacheAPISecureOrigin",
  "1249": "CSSAtRuleApply",
  "1250": "CSSSelectorPseudoAny",
  "1251": "PannerNodeSetVelocity",
  "1252": "DocumentAllItemNoArguments",
  "1253": "DocumentAllItemNamed",
  "1254": "DocumentAllItemIndexed",
  "1255": "DocumentAllItemIndexedWithNonNumber",
  "1256": "DocumentAllLegacyCallNoArguments",
  "1257": "DocumentAllLegacyCallNamed",
  "1258": "DocumentAllLegacyCallIndexed",
  "1259": "DocumentAllLegacyCallIndexedWithNonNumber",
  "1260": "DocumentAllLegacyCallTwoArguments",
  "1263": "HTMLLabelElementControlForNonFormAssociatedElement",
  "1265": "HTMLMediaElementLoadNetworkEmptyNotPaused",
  "1267": "V8Window_WebkitSpeechGrammar_ConstructorGetter",
  "1268": "V8Window_WebkitSpeechGrammarList_ConstructorGetter",
  "1269": "V8Window_WebkitSpeechRecognition_ConstructorGetter",
  "1270": "V8Window_WebkitSpeechRecognitionError_ConstructorGetter",
  "1271": "V8Window_WebkitSpeechRecognitionEvent_ConstructorGetter",
  "1272": "V8Window_SpeechSynthesis_AttributeGetter",
  "1273": "V8IDBFactory_WebkitGetDatabaseNames_Method",
  "1274": "ImageDocument",
  "1275": "ScriptPassesCSPDynamic",
  "1277": "CSPWithStrictDynamic",
  "1278": "ScrollAnchored",
  "1279": "AddEventListenerFourArguments",
  "1280": "RemoveEventListenerFourArguments",
  "1281": "InvalidReportUriDirectiveInMetaCSP",
  "1282": "InvalidSandboxDirectiveInMetaCSP",
  "1283": "InvalidFrameAncestorsDirectiveInMetaCSP",
  "1287": "SVGCalcModeDiscrete",
  "1288": "SVGCalcModeLinear",
  "1289": "SVGCalcModePaced",
  "1290": "SVGCalcModeSpline",
  "1291": "FormSubmissionStarted",
  "1292": "FormValidationStarted",
  "1293": "FormValidationAbortedSubmission",
  "1294": "FormValidationShowedMessage",
  "1295": "WebAnimationsEasingAsFunctionLinear",
  "1296": "WebAnimationsEasingAsFunctionOther",
  "1297": "V8Document_Images_AttributeGetter",
  "1298": "V8Document_Embeds_AttributeGetter",
  "1299": "V8Document_Plugins_AttributeGetter",
  "1300": "V8Document_Links_AttributeGetter",
  "1301": "V8Document_Forms_AttributeGetter",
  "1302": "V8Document_Scripts_AttributeGetter",
  "1303": "V8Document_Anchors_AttributeGetter",
  "1304": "V8Document_Applets_AttributeGetter",
  "1305": "XMLHttpRequestCrossOriginWithCredentials",
  "1306": "MediaStreamTrackRemote",
  "1307": "V8Node_IsConnected_AttributeGetter",
  "1308": "ShadowRootDelegatesFocus",
  "1309": "MixedShadowRootV0AndV1",
  "1310": "ImageDocumentInFrame",
  "1311": "MediaDocument",
  "1312": "MediaDocumentInFrame",
  "1313": "PluginDocument",
  "1314": "PluginDocumentInFrame",
  "1315": "SinkDocument",
  "1316": "SinkDocumentInFrame",
  "1317": "TextDocument",
  "1318": "TextDocumentInFrame",
  "1319": "ViewSourceDocument",
  "1320": "FileAPINativeLineEndings",
  "1321": "PointerEventAttributeCount",
  "1322": "CompositedReplication",
  "1323": "EncryptedMediaAllSelectedContentTypesHaveCodecs",
  "1324": "EncryptedMediaAllSelectedContentTypesMissingCodecs",
  "1325": "V8DataTransferItem_WebkitGetAsEntry_Method",
  "1326": "V8HTMLInputElement_WebkitEntries_AttributeGetter",
  "1327": "Entry_Filesystem_AttributeGetter_IsolatedFileSystem",
  "1328": "Entry_GetMetadata_Method_IsolatedFileSystem",
  "1329": "Entry_MoveTo_Method_IsolatedFileSystem",
  "1330": "Entry_CopyTo_Method_IsolatedFileSystem",
  "1331": "Entry_Remove_Method_IsolatedFileSystem",
  "1332": "Entry_GetParent_Method_IsolatedFileSystem",
  "1333": "Entry_ToURL_Method_IsolatedFileSystem",
  "1334": "During_Microtask_Alert",
  "1335": "During_Microtask_Confirm",
  "1336": "During_Microtask_Print",
  "1337": "During_Microtask_Prompt",
  "1338": "During_Microtask_SyncXHR",
  "1342": "CredentialManagerGetReturnedCredential",
  "1343": "GeolocationInsecureOriginDeprecatedNotRemoved",
  "1344": "GeolocationInsecureOriginIframeDeprecatedNotRemoved",
  "1345": "ProgressElementWithNoneAppearance",
  "1346": "ProgressElementWithProgressBarAppearance",
  "1347": "PointerEventAddListenerCount",
  "1348": "EventCancelBubbleAffected",
  "1349": "EventCancelBubbleWasChangedToTrue",
  "1350": "EventCancelBubbleWasChangedToFalse",
  "1351": "CSSValueAppearanceNone",
  "1352": "CSSValueAppearanceNotNone",
  "1353": "CSSValueAppearanceOthers",
  "1354": "CSSValueAppearanceButton",
  "1355": "CSSValueAppearanceCaret",
  "1356": "CSSValueAppearanceCheckbox",
  "1357": "CSSValueAppearanceMenulist",
  "1358": "CSSValueAppearanceMenulistButton",
  "1359": "CSSValueAppearanceListbox",
  "1360": "CSSValueAppearanceRadio",
  "1361": "CSSValueAppearanceSearchField",
  "1362": "CSSValueAppearanceTextField",
  "1363": "AudioContextCreatePannerAutomated",
  "1364": "PannerNodeSetPosition",
  "1365": "PannerNodeSetOrientation",
  "1366": "AudioListenerSetPosition",
  "1367": "AudioListenerSetOrientation",
  "1368": "IntersectionObserver_Constructor",
  "1369": "DurableStoragePersist",
  "1370": "DurableStoragePersisted",
  "1371": "DurableStorageEstimate",
  "1372": "UntrustedEventDefaultHandled",
  "1375": "CSSDeepCombinatorAndShadow",
  "1376": "OpacityWithPreserve3DQuirk",
  "1377": "CSSSelectorPseudoReadOnly",
  "1378": "CSSSelectorPseudoReadWrite",
  "1379": "UnloadHandler_Navigation",
  "1380": "TouchStartUserGestureUtilized",
  "1381": "TouchMoveUserGestureUtilized",
  "1382": "TouchEndDuringScrollUserGestureUtilized",
  "1383": "CSSSelectorPseudoDefined",
  "1384": "RTCPeerConnectionAddIceCandidatePromise",
  "1385": "RTCPeerConnectionAddIceCandidateLegacy",
  "1386": "RTCIceCandidateDefaultSdpMLineIndex",
  "1389": "MediaStreamConstraintsOldAndNew",
  "1390": "V8ArrayProtectorDirtied",
  "1391": "V8ArraySpeciesModified",
  "1392": "V8ArrayPrototypeConstructorModified",
  "1393": "V8ArrayInstanceProtoModified",
  "1394": "V8ArrayInstanceConstructorModified",
  "1395": "V8LegacyFunctionDeclaration",
  "1396": "V8RegExpPrototypeSourceGetter",
  "1397": "V8RegExpPrototypeOldFlagGetter",
  "1398": "V8DecimalWithLeadingZeroInStrictMode",
  "1399": "FormSubmissionNotInDocumentTree",
  "1400": "GetUserMediaPrefixed",
  "1401": "GetUserMediaLegacy",
  "1402": "GetUserMediaPromise",
  "1403": "CSSFilterFunctionNoArguments",
  "1404": "V8LegacyDateParser",
  "1405": "OpenSearchInsecureOriginInsecureTarget",
  "1406": "OpenSearchInsecureOriginSecureTarget",
  "1407": "OpenSearchSecureOriginInsecureTarget",
  "1408": "OpenSearchSecureOriginSecureTarget",
  "1409": "RegisterProtocolHandlerSecureOrigin",
  "1410": "RegisterProtocolHandlerInsecureOrigin",
  "1411": "CrossOriginWindowAlert",
  "1412": "CrossOriginWindowConfirm",
  "1413": "CrossOriginWindowPrompt",
  "1414": "CrossOriginWindowPrint",
  "1415": "MediaStreamOnActive",
  "1416": "MediaStreamOnInactive",
  "1417": "AddEventListenerPassiveTrue",
  "1418": "AddEventListenerPassiveFalse",
  "1419": "CSPReferrerDirective",
  "1420": "DocumentOpen",
  "1421": "ElementRequestPointerLockInShadow",
  "1422": "ShadowRootPointerLockElement",
  "1423": "DocumentPointerLockElementInV0Shadow",
  "1424": "TextAreaMaxLength",
  "1425": "TextAreaMinLength",
  "1426": "TopNavigationFromSubFrame",
  "1427": "PrefixedElementRequestFullscreenInShadow",
  "1428": "MediaSourceAbortRemove",
  "1429": "MediaSourceDurationTruncatingBuffered",
  "1430": "AudioContextCrossOriginIframe",
  "1431": "PointerEventSetCapture",
  "1432": "PointerEventDispatch",
  "1433": "MIDIMessageEventReceivedTime",
  "1434": "SummaryElementWithDisplayBlockAuthorRule",
  "1435": "V8MediaStream_Active_AttributeGetter",
  "1436": "BeforeInstallPromptEvent",
  "1437": "BeforeInstallPromptEventUserChoice",
  "1438": "BeforeInstallPromptEventPreventDefault",
  "1439": "BeforeInstallPromptEventPrompt",
  "1440": "ExecCommandAltersHTMLStructure",
  "1441": "SecureContextCheckPassed",
  "1442": "SecureContextCheckFailed",
  "1443": "SecureContextCheckForSandboxedOriginPassed",
  "1444": "SecureContextCheckForSandboxedOriginFailed",
  "1445": "V8DefineGetterOrSetterWouldThrow",
  "1446": "V8FunctionConstructorReturnedUndefined",
  "1447": "V8BroadcastChannel_Constructor",
  "1448": "V8BroadcastChannel_PostMessage_Method",
  "1449": "V8BroadcastChannel_Close_Method",
  "1450": "TouchStartFired",
  "1451": "MouseDownFired",
  "1452": "PointerDownFired",
  "1453": "PointerDownFiredForTouch",
  "1454": "PointerEventDispatchPointerDown",
  "1455": "SVGSMILBeginOrEndEventValue",
  "1456": "SVGSMILBeginOrEndSyncbaseValue",
  "1457": "SVGSMILElementInsertedAfterLoad",
  "1458": "V8VisualViewport_ScrollLeft_AttributeGetter",
  "1459": "V8VisualViewport_ScrollTop_AttributeGetter",
  "1460": "V8VisualViewport_PageX_AttributeGetter",
  "1461": "V8VisualViewport_PageY_AttributeGetter",
  "1462": "V8VisualViewport_ClientWidth_AttributeGetter",
  "1463": "V8VisualViewport_ClientHeight_AttributeGetter",
  "1464": "V8VisualViewport_Scale_AttributeGetter",
  "1465": "VisualViewportScrollFired",
  "1466": "VisualViewportResizeFired",
  "1467": "NodeGetRootNode",
  "1468": "SlotChangeEventAddListener",
  "1469": "CSSValueAppearanceButtonRendered",
  "1470": "CSSValueAppearanceButtonForAnchor",
  "1471": "CSSValueAppearanceButtonForButton",
  "1472": "CSSValueAppearanceButtonForOtherButtons",
  "1473": "CSSValueAppearanceTextFieldRendered",
  "1474": "CSSValueAppearanceTextFieldForSearch",
  "1475": "CSSValueAppearanceTextFieldForTextField",
  "1476": "RTCPeerConnectionGetStats",
  "1477": "SVGSMILAnimationAppliedEffect",
  "1478": "PerformanceResourceTimingSizes",
  "1479": "EventSourceDocument",
  "1480": "EventSourceWorker",
  "1481": "SingleOriginInTimingAllowOrigin",
  "1482": "MultipleOriginsInTimingAllowOrigin",
  "1483": "StarInTimingAllowOrigin",
  "1484": "SVGSMILAdditiveAnimation",
  "1485": "SendBeaconWithNonSimpleContentType",
  "1486": "ChromeLoadTimesRequestTime",
  "1487": "ChromeLoadTimesStartLoadTime",
  "1488": "ChromeLoadTimesCommitLoadTime",
  "1489": "ChromeLoadTimesFinishDocumentLoadTime",
  "1490": "ChromeLoadTimesFinishLoadTime",
  "1491": "ChromeLoadTimesFirstPaintTime",
  "1492": "ChromeLoadTimesFirstPaintAfterLoadTime",
  "1493": "ChromeLoadTimesNavigationType",
  "1494": "ChromeLoadTimesWasFetchedViaSpdy",
  "1495": "ChromeLoadTimesWasNpnNegotiated",
  "1496": "ChromeLoadTimesNpnNegotiatedProtocol",
  "1497": "ChromeLoadTimesWasAlternateProtocolAvailable",
  "1498": "ChromeLoadTimesConnectionInfo",
  "1499": "ChromeLoadTimesUnknown",
  "1500": "SVGViewElement",
  "1501": "WebShareShare",
  "1502": "AuxclickAddListenerCount",
  "1503": "HTMLCanvasElement",
  "1504": "SVGSMILAnimationElementTiming",
  "1505": "SVGSMILBeginEndAnimationElement",
  "1506": "SVGSMILPausing",
  "1507": "SVGSMILCurrentTime",
  "1508": "HTMLBodyElementOnSelectionChangeAttribute",
  "1509": "ForeignFetchInterception",
  "1510": "MapNameMatchingStrict",
  "1511": "MapNameMatchingASCIICaseless",
  "1512": "MapNameMatchingUnicodeLower",
  "1513": "RadioNameMatchingStrict",
  "1514": "RadioNameMatchingASCIICaseless",
  "1515": "RadioNameMatchingCaseFolding",
  "1517": "InputSelectionGettersThrow",
  "1519": "UsbGetDevices",
  "1520": "UsbRequestDevice",
  "1521": "UsbDeviceOpen",
  "1522": "UsbDeviceClose",
  "1523": "UsbDeviceSelectConfiguration",
  "1524": "UsbDeviceClaimInterface",
  "1525": "UsbDeviceReleaseInterface",
  "1526": "UsbDeviceSelectAlternateInterface",
  "1527": "UsbDeviceControlTransferIn",
  "1528": "UsbDeviceControlTransferOut",
  "1529": "UsbDeviceClearHalt",
  "1530": "UsbDeviceTransferIn",
  "1531": "UsbDeviceTransferOut",
  "1532": "UsbDeviceIsochronousTransferIn",
  "1533": "UsbDeviceIsochronousTransferOut",
  "1534": "UsbDeviceReset",
  "1535": "PointerEnterLeaveFired",
  "1536": "PointerOverOutFired",
  "1539": "DraggableAttribute",
  "1540": "CleanScriptElementWithNonce",
  "1541": "PotentiallyInjectedScriptElementWithNonce",
  "1542": "PendingStylesheetAddedAfterBodyStarted",
  "1543": "UntrustedMouseDownEventDispatchedToSelect",
  "1544": "BlockedSniffingAudioToScript",
  "1545": "BlockedSniffingVideoToScript",
  "1546": "BlockedSniffingCSVToScript",
  "1547": "MetaSetCookie",
  "1548": "MetaRefresh",
  "1549": "MetaSetCookieWhenCSPBlocksInlineScript",
  "1550": "MetaRefreshWhenCSPBlocksInlineScript",
  "1551": "MiddleClickAutoscrollStart",
  "1552": "ClipCssOfFixedPositionElement",
  "1553": "RTCPeerConnectionCreateOfferOptionsOfferToReceive",
  "1554": "DragAndDropScrollStart",
  "1555": "PresentationConnectionListConnectionAvailableEventListener",
  "1556": "WebAudioAutoplayCrossOriginIframe",
  "1557": "ScriptInvalidTypeOrLanguage",
  "1558": "VRGetDisplays",
  "1559": "VRPresent",
  "1560": "VRDeprecatedGetPose",
  "1561": "WebAudioAnalyserNode",
  "1562": "WebAudioAudioBuffer",
  "1563": "WebAudioAudioBufferSourceNode",
  "1564": "WebAudioBiquadFilterNode",
  "1565": "WebAudioChannelMergerNode",
  "1566": "WebAudioChannelSplitterNode",
  "1567": "WebAudioConvolverNode",
  "1568": "WebAudioDelayNode",
  "1569": "WebAudioDynamicsCompressorNode",
  "1570": "WebAudioGainNode",
  "1571": "WebAudioIIRFilterNode",
  "1572": "WebAudioMediaElementAudioSourceNode",
  "1573": "WebAudioOscillatorNode",
  "1574": "WebAudioPannerNode",
  "1575": "WebAudioPeriodicWave",
  "1576": "WebAudioStereoPannerNode",
  "1577": "WebAudioWaveShaperNode",
  "1578": "CSSZoomReset",
  "1579": "CSSZoomDocument",
  "1580": "PaymentAddressCareOf",
  "1581": "XSSAuditorBlockedScript",
  "1582": "XSSAuditorBlockedEntirePage",
  "1583": "XSSAuditorDisabled",
  "1584": "XSSAuditorEnabledFilter",
  "1585": "XSSAuditorEnabledBlock",
  "1586": "XSSAuditorInvalid",
  "1587": "SVGCursorElement",
  "1588": "SVGCursorElementHasClient",
  "1589": "TextInputEventOnInput",
  "1590": "TextInputEventOnTextArea",
  "1591": "TextInputEventOnContentEditable",
  "1592": "TextInputEventOnNotNode",
  "1593": "WebkitBeforeTextInsertedOnInput",
  "1594": "WebkitBeforeTextInsertedOnTextArea",
  "1595": "WebkitBeforeTextInsertedOnContentEditable",
  "1596": "WebkitBeforeTextInsertedOnNotNode",
  "1597": "WebkitEditableContentChangedOnInput",
  "1598": "WebkitEditableContentChangedOnTextArea",
  "1599": "WebkitEditableContentChangedOnContentEditable",
  "1600": "WebkitEditableContentChangedOnNotNode",
  "1601": "V8NavigatorUserMediaError_ConstraintName_AttributeGetter",
  "1602": "V8HTMLMediaElement_SrcObject_AttributeGetter",
  "1603": "V8HTMLMediaElement_SrcObject_AttributeSetter",
  "1604": "CreateObjectURLBlob",
  "1605": "CreateObjectURLMediaSource",
  "1606": "CreateObjectURLMediaStream",
  "1607": "DocumentCreateTouchWindowNull",
  "1608": "DocumentCreateTouchWindowWrongType",
  "1609": "DocumentCreateTouchTargetNull",
  "1610": "DocumentCreateTouchTargetWrongType",
  "1611": "DocumentCreateTouchLessThanSevenArguments",
  "1612": "DocumentCreateTouchMoreThanSevenArguments"
}

########################################################################################################################
#   Blink CSS feature names from https://cs.chromium.org/chromium/src/out/Debug/gen/blink/core/CSSPropertyNames.h
########################################################################################################################
BLINK_CSS_FEATURES = {
  "1": "CSSPropertyApplyAtRule",
  "2": "CSSPropertyVariable",
  "3": "CSSPropertyColor",
  "4": "CSSPropertyDirection",
  "5": "CSSPropertyFontFamily",
  "6": "CSSPropertyFontKerning",
  "7": "CSSPropertyFontSize",
  "8": "CSSPropertyFontSizeAdjust",
  "9": "CSSPropertyFontStretch",
  "10": "CSSPropertyFontStyle",
  "11": "CSSPropertyFontVariantLigatures",
  "12": "CSSPropertyFontVariantCaps",
  "13": "CSSPropertyFontVariantNumeric",
  "14": "CSSPropertyFontWeight",
  "15": "CSSPropertyFontFeatureSettings",
  "16": "CSSPropertyWebkitFontSmoothing",
  "17": "CSSPropertyWebkitLocale",
  "18": "CSSPropertyTextOrientation",
  "19": "CSSPropertyWebkitTextOrientation",
  "20": "CSSPropertyWritingMode",
  "21": "CSSPropertyWebkitWritingMode",
  "22": "CSSPropertyTextRendering",
  "23": "CSSPropertyZoom",
  "24": "CSSPropertyAlignContent",
  "25": "CSSPropertyAlignItems",
  "26": "CSSPropertyAlignmentBaseline",
  "27": "CSSPropertyAlignSelf",
  "28": "CSSPropertyAnimationDelay",
  "29": "CSSPropertyAnimationDirection",
  "30": "CSSPropertyAnimationDuration",
  "31": "CSSPropertyAnimationFillMode",
  "32": "CSSPropertyAnimationIterationCount",
  "33": "CSSPropertyAnimationName",
  "34": "CSSPropertyAnimationPlayState",
  "35": "CSSPropertyAnimationTimingFunction",
  "36": "CSSPropertyBackdropFilter",
  "37": "CSSPropertyBackfaceVisibility",
  "38": "CSSPropertyBackgroundAttachment",
  "39": "CSSPropertyBackgroundBlendMode",
  "40": "CSSPropertyBackgroundClip",
  "41": "CSSPropertyBackgroundColor",
  "42": "CSSPropertyBackgroundImage",
  "43": "CSSPropertyBackgroundOrigin",
  "44": "CSSPropertyBackgroundPositionX",
  "45": "CSSPropertyBackgroundPositionY",
  "46": "CSSPropertyBackgroundRepeatX",
  "47": "CSSPropertyBackgroundRepeatY",
  "48": "CSSPropertyBackgroundSize",
  "49": "CSSPropertyBaselineShift",
  "50": "CSSPropertyBorderBottomColor",
  "51": "CSSPropertyBorderBottomLeftRadius",
  "52": "CSSPropertyBorderBottomRightRadius",
  "53": "CSSPropertyBorderBottomStyle",
  "54": "CSSPropertyBorderBottomWidth",
  "55": "CSSPropertyBorderCollapse",
  "56": "CSSPropertyBorderImageOutset",
  "57": "CSSPropertyBorderImageRepeat",
  "58": "CSSPropertyBorderImageSlice",
  "59": "CSSPropertyBorderImageSource",
  "60": "CSSPropertyBorderImageWidth",
  "61": "CSSPropertyBorderLeftColor",
  "62": "CSSPropertyBorderLeftStyle",
  "63": "CSSPropertyBorderLeftWidth",
  "64": "CSSPropertyBorderRightColor",
  "65": "CSSPropertyBorderRightStyle",
  "66": "CSSPropertyBorderRightWidth",
  "67": "CSSPropertyBorderTopColor",
  "68": "CSSPropertyBorderTopLeftRadius",
  "69": "CSSPropertyBorderTopRightRadius",
  "70": "CSSPropertyBorderTopStyle",
  "71": "CSSPropertyBorderTopWidth",
  "72": "CSSPropertyBottom",
  "73": "CSSPropertyBoxShadow",
  "74": "CSSPropertyBoxSizing",
  "75": "CSSPropertyBreakAfter",
  "76": "CSSPropertyBreakBefore",
  "77": "CSSPropertyBreakInside",
  "78": "CSSPropertyBufferedRendering",
  "79": "CSSPropertyCaptionSide",
  "80": "CSSPropertyClear",
  "81": "CSSPropertyClip",
  "82": "CSSPropertyClipPath",
  "83": "CSSPropertyClipRule",
  "84": "CSSPropertyColorInterpolation",
  "85": "CSSPropertyColorInterpolationFilters",
  "86": "CSSPropertyColorRendering",
  "87": "CSSPropertyColumnFill",
  "88": "CSSPropertyContain",
  "89": "CSSPropertyContent",
  "90": "CSSPropertyCounterIncrement",
  "91": "CSSPropertyCounterReset",
  "92": "CSSPropertyCursor",
  "93": "CSSPropertyCx",
  "94": "CSSPropertyCy",
  "95": "CSSPropertyD",
  "96": "CSSPropertyDisplay",
  "97": "CSSPropertyDominantBaseline",
  "98": "CSSPropertyEmptyCells",
  "99": "CSSPropertyFill",
  "100": "CSSPropertyFillOpacity",
  "101": "CSSPropertyFillRule",
  "102": "CSSPropertyFilter",
  "103": "CSSPropertyFlexBasis",
  "104": "CSSPropertyFlexDirection",
  "105": "CSSPropertyFlexGrow",
  "106": "CSSPropertyFlexShrink",
  "107": "CSSPropertyFlexWrap",
  "108": "CSSPropertyFloat",
  "109": "CSSPropertyFloodColor",
  "110": "CSSPropertyFloodOpacity",
  "111": "CSSPropertyGridAutoColumns",
  "112": "CSSPropertyGridAutoFlow",
  "113": "CSSPropertyGridAutoRows",
  "114": "CSSPropertyGridColumnEnd",
  "115": "CSSPropertyGridColumnGap",
  "116": "CSSPropertyGridColumnStart",
  "117": "CSSPropertyGridRowEnd",
  "118": "CSSPropertyGridRowGap",
  "119": "CSSPropertyGridRowStart",
  "120": "CSSPropertyGridTemplateAreas",
  "121": "CSSPropertyGridTemplateColumns",
  "122": "CSSPropertyGridTemplateRows",
  "123": "CSSPropertyHeight",
  "124": "CSSPropertyHyphens",
  "125": "CSSPropertyImageRendering",
  "126": "CSSPropertyImageOrientation",
  "127": "CSSPropertyIsolation",
  "128": "CSSPropertyJustifyContent",
  "129": "CSSPropertyJustifyItems",
  "130": "CSSPropertyJustifySelf",
  "131": "CSSPropertyLeft",
  "132": "CSSPropertyLetterSpacing",
  "133": "CSSPropertyLightingColor",
  "134": "CSSPropertyLineHeight",
  "135": "CSSPropertyListStyleImage",
  "136": "CSSPropertyListStylePosition",
  "137": "CSSPropertyListStyleType",
  "138": "CSSPropertyMarginBottom",
  "139": "CSSPropertyMarginLeft",
  "140": "CSSPropertyMarginRight",
  "141": "CSSPropertyMarginTop",
  "142": "CSSPropertyMarkerEnd",
  "143": "CSSPropertyMarkerMid",
  "144": "CSSPropertyMarkerStart",
  "145": "CSSPropertyMask",
  "146": "CSSPropertyMaskSourceType",
  "147": "CSSPropertyMaskType",
  "148": "CSSPropertyMaxHeight",
  "149": "CSSPropertyMaxWidth",
  "150": "CSSPropertyMinHeight",
  "151": "CSSPropertyMinWidth",
  "152": "CSSPropertyMixBlendMode",
  "153": "CSSPropertyMotionOffset",
  "154": "CSSPropertyMotionPath",
  "155": "CSSPropertyMotionRotation",
  "156": "CSSPropertyObjectFit",
  "157": "CSSPropertyObjectPosition",
  "158": "CSSPropertyOpacity",
  "159": "CSSPropertyOrder",
  "160": "CSSPropertyOrphans",
  "161": "CSSPropertyOutlineColor",
  "162": "CSSPropertyOutlineOffset",
  "163": "CSSPropertyOutlineStyle",
  "164": "CSSPropertyOutlineWidth",
  "165": "CSSPropertyOverflowAnchor",
  "166": "CSSPropertyOverflowWrap",
  "167": "CSSPropertyOverflowX",
  "168": "CSSPropertyOverflowY",
  "169": "CSSPropertyPaddingBottom",
  "170": "CSSPropertyPaddingLeft",
  "171": "CSSPropertyPaddingRight",
  "172": "CSSPropertyPaddingTop",
  "173": "CSSPropertyPaintOrder",
  "174": "CSSPropertyPerspective",
  "175": "CSSPropertyPerspectiveOrigin",
  "176": "CSSPropertyPointerEvents",
  "177": "CSSPropertyPosition",
  "178": "CSSPropertyQuotes",
  "179": "CSSPropertyResize",
  "180": "CSSPropertyRight",
  "181": "CSSPropertyR",
  "182": "CSSPropertyRx",
  "183": "CSSPropertyRy",
  "184": "CSSPropertyScrollBehavior",
  "185": "CSSPropertyScrollSnapType",
  "186": "CSSPropertyScrollSnapPointsX",
  "187": "CSSPropertyScrollSnapPointsY",
  "188": "CSSPropertyScrollSnapDestination",
  "189": "CSSPropertyScrollSnapCoordinate",
  "190": "CSSPropertyShapeImageThreshold",
  "191": "CSSPropertyShapeMargin",
  "192": "CSSPropertyShapeOutside",
  "193": "CSSPropertyShapeRendering",
  "194": "CSSPropertySize",
  "195": "CSSPropertySnapHeight",
  "196": "CSSPropertySpeak",
  "197": "CSSPropertyStopColor",
  "198": "CSSPropertyStopOpacity",
  "199": "CSSPropertyStroke",
  "200": "CSSPropertyStrokeDasharray",
  "201": "CSSPropertyStrokeDashoffset",
  "202": "CSSPropertyStrokeLinecap",
  "203": "CSSPropertyStrokeLinejoin",
  "204": "CSSPropertyStrokeMiterlimit",
  "205": "CSSPropertyStrokeOpacity",
  "206": "CSSPropertyStrokeWidth",
  "207": "CSSPropertyTableLayout",
  "208": "CSSPropertyTabSize",
  "209": "CSSPropertyTextAlign",
  "210": "CSSPropertyTextAlignLast",
  "211": "CSSPropertyTextAnchor",
  "212": "CSSPropertyTextCombineUpright",
  "213": "CSSPropertyTextDecoration",
  "214": "CSSPropertyTextDecorationColor",
  "215": "CSSPropertyTextDecorationLine",
  "216": "CSSPropertyTextDecorationStyle",
  "217": "CSSPropertyTextIndent",
  "218": "CSSPropertyTextJustify",
  "219": "CSSPropertyTextOverflow",
  "220": "CSSPropertyTextShadow",
  "221": "CSSPropertyTextSizeAdjust",
  "222": "CSSPropertyTextTransform",
  "223": "CSSPropertyTextUnderlinePosition",
  "224": "CSSPropertyTop",
  "225": "CSSPropertyTouchAction",
  "226": "CSSPropertyTransform",
  "227": "CSSPropertyTransformOrigin",
  "228": "CSSPropertyTransformStyle",
  "229": "CSSPropertyTranslate",
  "230": "CSSPropertyRotate",
  "231": "CSSPropertyScale",
  "232": "CSSPropertyTransitionDelay",
  "233": "CSSPropertyTransitionDuration",
  "234": "CSSPropertyTransitionProperty",
  "235": "CSSPropertyTransitionTimingFunction",
  "236": "CSSPropertyUnicodeBidi",
  "237": "CSSPropertyVectorEffect",
  "238": "CSSPropertyVerticalAlign",
  "239": "CSSPropertyVisibility",
  "240": "CSSPropertyX",
  "241": "CSSPropertyY",
  "242": "CSSPropertyWebkitAppearance",
  "243": "CSSPropertyWebkitAppRegion",
  "244": "CSSPropertyWebkitBackgroundClip",
  "245": "CSSPropertyWebkitBackgroundOrigin",
  "246": "CSSPropertyWebkitBorderHorizontalSpacing",
  "247": "CSSPropertyWebkitBorderImage",
  "248": "CSSPropertyWebkitBorderVerticalSpacing",
  "249": "CSSPropertyWebkitBoxAlign",
  "250": "CSSPropertyWebkitBoxDecorationBreak",
  "251": "CSSPropertyWebkitBoxDirection",
  "252": "CSSPropertyWebkitBoxFlex",
  "253": "CSSPropertyWebkitBoxFlexGroup",
  "254": "CSSPropertyWebkitBoxLines",
  "255": "CSSPropertyWebkitBoxOrdinalGroup",
  "256": "CSSPropertyWebkitBoxOrient",
  "257": "CSSPropertyWebkitBoxPack",
  "258": "CSSPropertyWebkitBoxReflect",
  "259": "CSSPropertyWebkitClipPath",
  "260": "CSSPropertyColumnCount",
  "261": "CSSPropertyColumnGap",
  "262": "CSSPropertyColumnRuleColor",
  "263": "CSSPropertyColumnRuleStyle",
  "264": "CSSPropertyColumnRuleWidth",
  "265": "CSSPropertyColumnSpan",
  "266": "CSSPropertyColumnWidth",
  "267": "CSSPropertyWebkitHighlight",
  "268": "CSSPropertyWebkitHyphenateCharacter",
  "269": "CSSPropertyWebkitLineBreak",
  "270": "CSSPropertyWebkitLineClamp",
  "271": "CSSPropertyWebkitMarginAfterCollapse",
  "272": "CSSPropertyWebkitMarginBeforeCollapse",
  "273": "CSSPropertyWebkitMarginBottomCollapse",
  "274": "CSSPropertyWebkitMarginTopCollapse",
  "275": "CSSPropertyWebkitMaskBoxImageOutset",
  "276": "CSSPropertyWebkitMaskBoxImageRepeat",
  "277": "CSSPropertyWebkitMaskBoxImageSlice",
  "278": "CSSPropertyWebkitMaskBoxImageSource",
  "279": "CSSPropertyWebkitMaskBoxImageWidth",
  "280": "CSSPropertyWebkitMaskClip",
  "281": "CSSPropertyWebkitMaskComposite",
  "282": "CSSPropertyWebkitMaskImage",
  "283": "CSSPropertyWebkitMaskOrigin",
  "284": "CSSPropertyWebkitMaskPositionX",
  "285": "CSSPropertyWebkitMaskPositionY",
  "286": "CSSPropertyWebkitMaskRepeatX",
  "287": "CSSPropertyWebkitMaskRepeatY",
  "288": "CSSPropertyWebkitMaskSize",
  "289": "CSSPropertyWebkitPerspectiveOriginX",
  "290": "CSSPropertyWebkitPerspectiveOriginY",
  "291": "CSSPropertyWebkitPrintColorAdjust",
  "292": "CSSPropertyWebkitRtlOrdering",
  "293": "CSSPropertyWebkitRubyPosition",
  "294": "CSSPropertyWebkitTapHighlightColor",
  "295": "CSSPropertyWebkitTextCombine",
  "296": "CSSPropertyWebkitTextEmphasisColor",
  "297": "CSSPropertyWebkitTextEmphasisPosition",
  "298": "CSSPropertyWebkitTextEmphasisStyle",
  "299": "CSSPropertyWebkitTextFillColor",
  "300": "CSSPropertyWebkitTextSecurity",
  "301": "CSSPropertyWebkitTextStrokeColor",
  "302": "CSSPropertyWebkitTextStrokeWidth",
  "303": "CSSPropertyWebkitTransformOriginX",
  "304": "CSSPropertyWebkitTransformOriginY",
  "305": "CSSPropertyWebkitTransformOriginZ",
  "306": "CSSPropertyWebkitUserDrag",
  "307": "CSSPropertyWebkitUserModify",
  "308": "CSSPropertyWebkitUserSelect",
  "309": "CSSPropertyWhiteSpace",
  "310": "CSSPropertyWidows",
  "311": "CSSPropertyWidth",
  "312": "CSSPropertyWillChange",
  "313": "CSSPropertyWordBreak",
  "314": "CSSPropertyWordSpacing",
  "315": "CSSPropertyWordWrap",
  "316": "CSSPropertyZIndex",
  "317": "CSSPropertyWebkitBorderEndColor",
  "318": "CSSPropertyWebkitBorderEndStyle",
  "319": "CSSPropertyWebkitBorderEndWidth",
  "320": "CSSPropertyWebkitBorderStartColor",
  "321": "CSSPropertyWebkitBorderStartStyle",
  "322": "CSSPropertyWebkitBorderStartWidth",
  "323": "CSSPropertyWebkitBorderBeforeColor",
  "324": "CSSPropertyWebkitBorderBeforeStyle",
  "325": "CSSPropertyWebkitBorderBeforeWidth",
  "326": "CSSPropertyWebkitBorderAfterColor",
  "327": "CSSPropertyWebkitBorderAfterStyle",
  "328": "CSSPropertyWebkitBorderAfterWidth",
  "329": "CSSPropertyWebkitMarginEnd",
  "330": "CSSPropertyWebkitMarginStart",
  "331": "CSSPropertyWebkitMarginBefore",
  "332": "CSSPropertyWebkitMarginAfter",
  "333": "CSSPropertyWebkitPaddingEnd",
  "334": "CSSPropertyWebkitPaddingStart",
  "335": "CSSPropertyWebkitPaddingBefore",
  "336": "CSSPropertyWebkitPaddingAfter",
  "337": "CSSPropertyWebkitLogicalWidth",
  "338": "CSSPropertyWebkitLogicalHeight",
  "339": "CSSPropertyWebkitMinLogicalWidth",
  "340": "CSSPropertyWebkitMinLogicalHeight",
  "341": "CSSPropertyWebkitMaxLogicalWidth",
  "342": "CSSPropertyWebkitMaxLogicalHeight",
  "343": "CSSPropertyAll",
  "344": "CSSPropertyPage",
  "345": "CSSPropertyWebkitFontSizeDelta",
  "346": "CSSPropertyWebkitTextDecorationsInEffect",
  "347": "CSSPropertyFontDisplay",
  "348": "CSSPropertyMaxZoom",
  "349": "CSSPropertyMinZoom",
  "350": "CSSPropertyOrientation",
  "351": "CSSPropertySrc",
  "352": "CSSPropertyUnicodeRange",
  "353": "CSSPropertyUserZoom",
  "354": "CSSPropertyAnimation",
  "355": "CSSPropertyBackground",
  "356": "CSSPropertyBackgroundPosition",
  "357": "CSSPropertyBackgroundRepeat",
  "358": "CSSPropertyBorder",
  "359": "CSSPropertyBorderBottom",
  "360": "CSSPropertyBorderColor",
  "361": "CSSPropertyBorderImage",
  "362": "CSSPropertyBorderLeft",
  "363": "CSSPropertyBorderRadius",
  "364": "CSSPropertyBorderRight",
  "365": "CSSPropertyBorderSpacing",
  "366": "CSSPropertyBorderStyle",
  "367": "CSSPropertyBorderTop",
  "368": "CSSPropertyBorderWidth",
  "369": "CSSPropertyFlex",
  "370": "CSSPropertyFlexFlow",
  "371": "CSSPropertyFont",
  "372": "CSSPropertyFontVariant",
  "373": "CSSPropertyGrid",
  "374": "CSSPropertyGridArea",
  "375": "CSSPropertyGridColumn",
  "376": "CSSPropertyGridGap",
  "377": "CSSPropertyGridRow",
  "378": "CSSPropertyGridTemplate",
  "379": "CSSPropertyListStyle",
  "380": "CSSPropertyMargin",
  "381": "CSSPropertyMarker",
  "382": "CSSPropertyMotion",
  "383": "CSSPropertyOutline",
  "384": "CSSPropertyOverflow",
  "385": "CSSPropertyPadding",
  "386": "CSSPropertyPageBreakAfter",
  "387": "CSSPropertyPageBreakBefore",
  "388": "CSSPropertyPageBreakInside",
  "389": "CSSPropertyTransition",
  "390": "CSSPropertyWebkitBorderAfter",
  "391": "CSSPropertyWebkitBorderBefore",
  "392": "CSSPropertyWebkitBorderEnd",
  "393": "CSSPropertyWebkitBorderStart",
  "394": "CSSPropertyWebkitColumnBreakAfter",
  "395": "CSSPropertyWebkitColumnBreakBefore",
  "396": "CSSPropertyWebkitColumnBreakInside",
  "397": "CSSPropertyColumnRule",
  "398": "CSSPropertyColumns",
  "399": "CSSPropertyWebkitMarginCollapse",
  "400": "CSSPropertyWebkitMask",
  "401": "CSSPropertyWebkitMaskBoxImage",
  "402": "CSSPropertyWebkitMaskPosition",
  "403": "CSSPropertyWebkitMaskRepeat",
  "404": "CSSPropertyWebkitTextEmphasis",
  "405": "CSSPropertyWebkitTextStroke",
  "591": "CSSPropertyAliasEpubCaptionSide",
  "807": "CSSPropertyAliasEpubTextCombine",
  "916": "CSSPropertyAliasEpubTextEmphasis",
  "808": "CSSPropertyAliasEpubTextEmphasisColor",
  "810": "CSSPropertyAliasEpubTextEmphasisStyle",
  "531": "CSSPropertyAliasEpubTextOrientation",
  "734": "CSSPropertyAliasEpubTextTransform",
  "825": "CSSPropertyAliasEpubWordBreak",
  "533": "CSSPropertyAliasEpubWritingMode",
  "536": "CSSPropertyAliasWebkitAlignContent",
  "537": "CSSPropertyAliasWebkitAlignItems",
  "539": "CSSPropertyAliasWebkitAlignSelf",
  "866": "CSSPropertyAliasWebkitAnimation",
  "540": "CSSPropertyAliasWebkitAnimationDelay",
  "541": "CSSPropertyAliasWebkitAnimationDirection",
  "542": "CSSPropertyAliasWebkitAnimationDuration",
  "543": "CSSPropertyAliasWebkitAnimationFillMode",
  "544": "CSSPropertyAliasWebkitAnimationIterationCount",
  "545": "CSSPropertyAliasWebkitAnimationName",
  "546": "CSSPropertyAliasWebkitAnimationPlayState",
  "547": "CSSPropertyAliasWebkitAnimationTimingFunction",
  "549": "CSSPropertyAliasWebkitBackfaceVisibility",
  "560": "CSSPropertyAliasWebkitBackgroundSize",
  "563": "CSSPropertyAliasWebkitBorderBottomLeftRadius",
  "564": "CSSPropertyAliasWebkitBorderBottomRightRadius",
  "875": "CSSPropertyAliasWebkitBorderRadius",
  "580": "CSSPropertyAliasWebkitBorderTopLeftRadius",
  "581": "CSSPropertyAliasWebkitBorderTopRightRadius",
  "585": "CSSPropertyAliasWebkitBoxShadow",
  "586": "CSSPropertyAliasWebkitBoxSizing",
  "772": "CSSPropertyAliasWebkitColumnCount",
  "773": "CSSPropertyAliasWebkitColumnGap",
  "909": "CSSPropertyAliasWebkitColumnRule",
  "774": "CSSPropertyAliasWebkitColumnRuleColor",
  "775": "CSSPropertyAliasWebkitColumnRuleStyle",
  "776": "CSSPropertyAliasWebkitColumnRuleWidth",
  "777": "CSSPropertyAliasWebkitColumnSpan",
  "778": "CSSPropertyAliasWebkitColumnWidth",
  "910": "CSSPropertyAliasWebkitColumns",
  "614": "CSSPropertyAliasWebkitFilter",
  "881": "CSSPropertyAliasWebkitFlex",
  "615": "CSSPropertyAliasWebkitFlexBasis",
  "616": "CSSPropertyAliasWebkitFlexDirection",
  "882": "CSSPropertyAliasWebkitFlexFlow",
  "617": "CSSPropertyAliasWebkitFlexGrow",
  "618": "CSSPropertyAliasWebkitFlexShrink",
  "619": "CSSPropertyAliasWebkitFlexWrap",
  "527": "CSSPropertyAliasWebkitFontFeatureSettings",
  "640": "CSSPropertyAliasWebkitJustifyContent",
  "670": "CSSPropertyAliasWebkitOpacity",
  "671": "CSSPropertyAliasWebkitOrder",
  "686": "CSSPropertyAliasWebkitPerspective",
  "687": "CSSPropertyAliasWebkitPerspectiveOrigin",
  "702": "CSSPropertyAliasWebkitShapeImageThreshold",
  "703": "CSSPropertyAliasWebkitShapeMargin",
  "704": "CSSPropertyAliasWebkitShapeOutside",
  "733": "CSSPropertyAliasWebkitTextSizeAdjust",
  "738": "CSSPropertyAliasWebkitTransform",
  "739": "CSSPropertyAliasWebkitTransformOrigin",
  "740": "CSSPropertyAliasWebkitTransformStyle",
  "901": "CSSPropertyAliasWebkitTransition",
  "744": "CSSPropertyAliasWebkitTransitionDelay",
  "745": "CSSPropertyAliasWebkitTransitionDuration",
  "746": "CSSPropertyAliasWebkitTransitionProperty",
  "747": "CSSPropertyAliasWebkitTransitionTimingFunction",
  "748": "CSSPropertyAliasWebkitTransitionProperty",
  "749": "CSSPropertyAliasWebkitTransitionTimingFunction",
  "821": "CSSPropertyAliasWebkitUserSelect"
}


if '__main__' == __name__:
#  import cProfile
#  cProfile.run('main()', None, 2)
  main()
