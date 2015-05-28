#!/usr/bin/env python
# encoding: utf-8

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.


import psycopg2
import pandas as pd
import ujson as json
import argparse

from datetime import datetime
from moztelemetry.spark import Histogram
from boto.s3.connection import S3Connection


def submit_aggregates(aggregates):
    _preparedb()
    aggregates.map(_update_partial_aggregate).count()
    _vacuumdb()


def _preparedb():
    conn = _create_connection()
    cursor = conn.cursor()
    query = """
create or replace function aggregate_arrays(acc bigint[], x jsonb) returns bigint[] as $$
declare
   i int;
   tmp bigint;
begin
for i in 0 .. json_array_length(x::json) - 1
loop
   if acc[i + 1] is NULL then
       acc[i + 1] = 0;
   end if;
   tmp := x->i;
   acc[i + 1] := acc[i + 1] + tmp;
end loop;
return acc;
end
$$ language plpgsql strict immutable;

drop aggregate if exists aggregate_histograms(jsonb);
create aggregate aggregate_histograms ( jsonb ) (
    sfunc = aggregate_arrays, stype = bigint[], initcond = '{}'
);

create or replace function add_buildid_metric(channel text, version text, buildid text, dimensions jsonb, histogram jsonb) returns void as $$
declare
    tablename text;
    table_exists bool;
    temporary text;
begin
    tablename := channel || '_' || version || '_' || buildid;
    -- Check if table exists and if not create one
    table_exists := (select exists (select 1 from information_schema.tables where table_schema = 'public' and table_name = tablename));
    if not table_exists then
        execute 'create table ' || tablename || '(id serial primary key) inherits (telemetry_aggregates_buildid)';
        execute 'create index on ' || tablename || ' using GIN (dimensions jsonb_path_ops)';
    end if;
    -- Check if the document already exists and update it, if not create one
    execute 'with upsert as (update ' || tablename || ' as t
                             set histogram = array_to_json((select aggregate_histograms(v) from (values (1, t.histogram), (2, $1)) as t (k, v)))::jsonb
                             where t.dimensions @> $2
                             returning t.*)
             insert into ' || tablename || ' (dimensions, histogram)
                    select * from (values ($2, $1)) as t
                    where not exists (select 1 from upsert)'
             using histogram, dimensions;
end
$$ language plpgsql strict;

create table if not exists telemetry_aggregates_buildid (dimensions jsonb, histogram jsonb);
    """

    cursor.execute(query)


def _create_connection(autocommit=True):
    s3 = S3Connection()
    config = s3.get_bucket("telemetry-spark-emr").get_key("aggregator_credentials").get_contents_as_string()
    config = json.loads(config)    
    conn = psycopg2.connect(dbname=config["dbname"], user=config["user"], password=config["password"], host=config["host"])   

    if autocommit:
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    return conn


def _get_complete_histogram(metric, values):
    if metric.startswith("SIMPLE_"):
        histogram = values  # histogram is already complete
    else:
        histogram = Histogram(metric, {"values": values}).get_value(autocast=False).values

    return map(int, list(histogram))


def _commit_partial_aggregate(cursor, aggregate):        
    key, metrics = aggregate
    channel, version, build_id, application, architecture, revision, os, os_version = key

    dimensions = {"application": application,
                  "architecture": architecture,
                  "revision": revision,
                  "os": os,
                  "os_version": os_version}

    for metric, payload in metrics.iteritems():
        metric, label, child = metric
        label = label.replace("'", ""); # Postgres doesn't like quotes

        dimensions["metric"] = metric
        dimensions["label"] = label
        dimensions["child"] = child

        try:
            histogram = _get_complete_histogram(metric, payload["histogram"]) + [payload["count"]]  # Append count at the end
        except KeyError as e:  # TODO: use revision service once it's ready 
            continue

        cursor.execute("select add_buildid_metric('{}', '{}', '{}', '{}', '{}')".format(channel,
                                                                                        version,
                                                                                        build_id,
                                                                                        json.dumps(dimensions),
                                                                                        json.dumps(histogram)))

def _update_partial_aggregate(aggregate):
    conn = _create_connection()
    cursor = conn.cursor()

    try:
        return _commit_partial_aggregate(cursor, aggregate)
    except psycopg2.IntegrityError as e:
        #see: http://www.postgresql.org/message-id/CA+TgmoZAdYVtwBfp1FL2sMZbiHCWT4UPrzRLNnX1Nb30Ku3-gg@mail.gmail.com
        return _commit_partial_aggregate(cursor, aggregate)

    conn.close()


def _vacuumdb():
    conn = _create_connection()
    old_isolation_level = conn.isolation_level
    conn.set_isolation_level(0)
    cursor = conn.cursor()
    cursor.execute("vacuum")
    conn.set_isolation_level(old_isolation_level)
    conn.close()
