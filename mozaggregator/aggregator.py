#!/usr/bin/env python
# encoding: utf-8

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import itertools
import binascii

from moztelemetry.spark import get_pings
from moztelemetry.histogram import cached_exponential_buckets
from collections import defaultdict


_exponential_index = cached_exponential_buckets(1, 30000, 50)


def aggregate_metrics(sc, channel, submission_date, fraction=1):
    pings = get_pings(sc, channel=channel, submission_date=submission_date, doc_type="saved_session", schema="v4", fraction=fraction)

    trimmed = pings.filter(_sample_clients).map(_map_ping_to_dimensions)
    return trimmed.aggregateByKey(defaultdict(dict), _aggregate_pings, _aggregate_aggregates)


def _sample_clients(ping):
    client_id = ping.get("clientId", None)
    channel = ping["application"]["channel"]
    percentage = {"nightly": 100,
                  "aurora": 100}
    return client_id and ((binascii.crc32(client_id) % 100) < percentage[channel])


def _extract_histograms(state, payload, is_child=False):
    histograms = payload.get("histograms", {})
    _extract_main_histograms(state, histograms, is_child)

    keyed_histograms = payload.get("keyedHistograms", {})
    for name, histograms in keyed_histograms.iteritems():
        _extract_keyed_histograms(state, name, histograms, is_child)


def _extract_main_histograms(state, histograms, is_child):
    for histogram_name, histogram in histograms.iteritems():
        accessor = (histogram_name, u"", is_child)
        aggregated_histogram = state[accessor]["histogram"] = state[accessor].get("histogram", {})
        state[accessor]["count"] = state[accessor].get("count", 0) + 1

        for k, v in histogram["values"].iteritems():
            aggregated_histogram[k] = aggregated_histogram.get(k, 0) + int(v)


def _extract_keyed_histograms(state, histogram_name, histograms, is_child):
    for key, histogram in histograms.iteritems():
        accessor = (histogram_name, key, is_child)
        aggregated_histogram = state[accessor]["histogram"] = state[accessor].get("histogram", {})
        state[accessor]["count"] = state[accessor].get("count", 0) + 1

        for k, v in histogram["values"].iteritems():
            aggregated_histogram[k] = aggregated_histogram.get(k, 0) + int(v)


def _extract_simple_measures(state, simple):
    for name, value in simple.iteritems():
        if type(value) == dict:
            for sub_name, sub_value in value.iteritems():
                if type(sub_value) in (int, float, long):
                    _extract_simple_measure(state, u"SIMPLE_MEASURES_{}_{}".format(name.upper(), sub_name.upper()), sub_value)
        elif type(value) in (int, float, long):
            _extract_simple_measure(state, u"SIMPLE_MEASURES_{}".format(name.upper()), value)


def _extract_simple_measure(state, name, value):    
    accessor = (name, u"", False)
    aggregated_histogram = state[accessor]["histogram"] = state[accessor].get("histogram", {})
    state[accessor]["count"] = state[accessor].get("count", 0) + 1

    for bucket in reversed(_exponential_index):
        if value >= bucket:
            aggregated_histogram[bucket] = aggregated_histogram.get(bucket, 0) + 1
            return

    # Underflow
    first_index = _exponential_index[0]
    aggregated_histogram[first_index] = aggregated_histogram.get(first_index, 0) + 1


def _extract_children_histograms(state, payload):
    child_payloads = payload.get("childPayloads", {})
    for child in child_payloads:
        _extract_histograms(state, child, True)

def _aggregate_ping(state, ping):
    _extract_histograms(state, ping["payload"])
    _extract_simple_measures(state, ping["payload"].get("simpleMeasurements", {}))
    _extract_children_histograms(state, ping["payload"])
    return state


def _trim_ping(ping):
    payload = {k: v for k, v in ping["payload"].iteritems() if k in ["histograms", "keyedHistograms", "info", "simpleMeasurements"]}
    return {"clientId": ping["clientId"],
            "meta": ping["meta"],
            "environment": ping["environment"],
            "application": ping["application"],
            "payload": payload}


def _aggregate_pings(state, ping):
    return _aggregate_ping(state, ping)


def _aggregate_aggregates(agg1, agg2):
    for metric, payload in agg2.iteritems():
        if metric == "count":
            continue

        if metric not in agg1:
            agg1[metric] = payload

        agg1[metric]["count"] += payload["count"]

        for k, v in payload["histogram"].iteritems():
            agg1[metric]["histogram"][k] = agg1[metric]["histogram"].get(k, 0) + v

    return agg1


def _map_ping_to_dimensions(ping):
    submission_date = ping["meta"]["submissionDate"]
    channel = ping["application"]["channel"]
    version = ping["application"]["version"].split('.')[0]
    build_id = ping["application"]["buildId"][:8]
    application = ping["application"]["name"]
    architecture = ping["application"]["architecture"]
    revision = ping["payload"]["info"]["revision"].split('/')[-1]
    os = ping["environment"]["system"]["os"]["name"]
    os_version = ping["environment"]["system"]["os"]["version"]
    if os == "Linux":
        os_version = str(os_version)[:3]

    return ((submission_date, channel, version, build_id, application, architecture, revision, os, os_version), ping)
