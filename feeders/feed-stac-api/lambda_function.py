import argparse
import boto3
import datetime
import json
import logging
import math
import os
import requests
import sys
import time
import uuid

import os.path as op

from boto3utils import s3
from copy import deepcopy
from cirruslib.utils import submit_batch_job
from dateutil.parser import parse
from satsearch import Search, config


# configure logger - CRITICAL, ERROR, WARNING, INFO, DEBUG
logger = logging.getLogger(__name__)
logger.setLevel(os.getenv('CIRRUS_LOG_LEVEL', 'DEBUG'))

# add logs for sat-search
logging.getLogger('satsearch').setLevel(os.getenv('CIRRUS_LOG_LEVEL', 'INFO'))
logger.addHandler(logging.StreamHandler())

# envvars
SNS_TOPIC = os.getenv('CIRRUS_QUEUE_TOPIC_ARN')
CATALOG_BUCKET = os.getenv('CIRRUS_CATALOG_BUCKET')
CIRRUS_STACK = os.getenv('CIRRUS_STACK')
MAX_ITEMS_REQUEST = 5000

# AWS clients
BATCH_CLIENT = boto3.client('batch')
SNS_CLIENT = boto3.client('sns')

# Process configuration
'''
{
    "url": "https://stac-api-endpoint",
    "search": {
        <stac-api-search-params>
    },
    "sleep": 10,
    "process": {
        <process-block>
    }
}
'''


def split_request(params, nbatches):
    dates = params.get('datetime', '').split('/')
    
    if len(dates) != 2:
        msg = "Do not know how to split up request without daterange"
        logger.error(msg)
        raise Exception(msg)
    start_date = parse(dates[0])
    if dates[1] == "now":
        stop_date = datetime.datetime.now()
    else:
        stop_date = parse(dates[1])
    td = stop_date - start_date
    days_per_batch = math.ceil(td.days/nbatches)
    ranges = []
    for i in range(0, nbatches-1):
        dt1 = start_date + datetime.timedelta(days=days_per_batch*i)
        dt2 = dt1 + datetime.timedelta(days=days_per_batch) - datetime.timedelta(seconds=1)
        ranges.append([dt1, dt2])
    # insert last one
    ranges.append([
        ranges[-1][1] + datetime.timedelta(seconds=1),
        stop_date
    ])

    requests = []
    for r in ranges:
        request = deepcopy(params)
        request["datetime"] = f"{r[0].strftime('%Y-%m-%d')}/{r[1].strftime('%Y-%m-%d')}"
        logger.debug(f"Split date range: {request['datetime']}")
        yield request
    

def run(params, url, sleep=None):
    search = Search(api_url=url, **params)
    logger.debug(f"Searching {url}")    
    found = search.found()
    logger.debug(f"Total items found: {found}")
 
    if found < MAX_ITEMS_REQUEST:
        logger.info(f"Making single request for {found} items")
        items = search.items()
        for i, item in enumerate(items):
            resp = SNS_CLIENT.publish(TopicArn=SNS_TOPIC, Message=json.dumps(item._data))
            if (i % 500) == 0:
                logger.debug(f"Added {i+1} items to Cirrus")
            #if resp['StatusCode'] != 200:
            #    raise Exception("Unable to publish")
            if sleep:
                time.sleep(sleep)
        logger.debug(f"Published {len(items)} items to {SNS_TOPIC}")
    else:
        # bisection
        nbatches = 2
        logger.info(f"Too many Items for single request, splitting into {nbatches} batches by date range")
        for params in split_request(params, nbatches):
            run(params, url)


def lambda_handler(event, context={}):
    logger.debug('Event: %s' % json.dumps(event))

    # if this is batch, output to stdout
    if not hasattr(context, "invoked_function_arn"):
        logger.addHandler(logging.StreamHandler())

    # parse input
    #s3urls = event['s3urls']
    #suffix = event.get('suffix', 'json')
    #credentials = event.get('credentials', {})
    #requester_pays = credentials.pop('requester_pays', False)

    ######
    url = event.get('url')
    params = event.get('search', {})
    max_items_batch = event.get('max_items_batch', 15000)
    sleep = event.get('sleep', None)

    # search API
    search = Search(api_url=url, **params)
    logger.debug(f"Searching {url}")

    found = search.found()
    logger.debug(f"Total items found: {found}")

    if found <= MAX_ITEMS_REQUEST:
        return run(params, url, sleep=sleep)
    elif hasattr(context, "invoked_function_arn"):
        nbatches = int(found / max_items_batch) + 1
        if nbatches == 1:
            submit_batch_job(event, context.invoked_function_arn)
        else:
            for request in split_request(params, nbatches):
                event['search'] = request
                submit_batch_job(event, context.invoked_function_arn)
        logger.info(f"Submitted {nbatches} batches")
        return
    else:
        run(params, url, sleep=sleep)


def parse_args(args):
    desc = 'feeder'
    dhf = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(description=desc, formatter_class=dhf)
    parser.add_argument('payload', help='Payload file')
    #parser.add_argument('--source_profile', help='Name of AWS profile to use to get data', default=None)
    #parser.add_argument('--workdir', help='Work directory', default='')
    #parser.add_argument('--queue', help='Name of Cirrus Queue Lambda', default=None)
    #parser.add_argument('--output_url', help='S3 URL prefix for uploading data', default=None)
    
    #parser.add_argument('--cirrus_profile', help='Name of AWS profile to use for queuing to Cirrus', default=None)

    return vars(parser.parse_args(args))


def cli():
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    args = parse_args(sys.argv[1:])
    with open(args['payload']) as f:
        payload = json.loads(f.read())
    #import pdb; pdb.set_trace()
    lambda_handler(payload)


if __name__ == "__main__":
    cli()
