# import json

# def lambda_handler(event, context):
#     # TODO implement
#     return {
#         'statusCode': 200,
#         'body': json.dumps('Hello from Lambda!')
#     }


import json
import base64
import subprocess
import os
import sys
from datetime import datetime
import time

import boto3

print(f'boto3 version: {boto3.__version__}')

try:
    sm = boto3.Session().client(service_name='sagemaker')
    sm_fs = boto3.Session().client(service_name='sagemaker-featurestore-runtime')
except:
    print(f'Failed while connecting to SageMaker Feature Store')
    print(f'Unexpected error: {sys.exc_info()[0]}')


# Read Environment Vars
CC_AGG_FEATURE_GROUP = os.environ['CC_AGG_FEATURE_GROUP_NAME']


def update_agg(fg_name, cc_num, avg_amt_last_10m, num_trans_last_10m):

    # needs to match agg_short term feature store:

    # - `orders_last_5m`
    # - `page_views_last_5m`
    # - `clicks_last_5m`
    # - `user_id`
    # - `event_time`

    record = [{'FeatureName':'cc_num', 'ValueAsString': str(cc_num)},
              {'FeatureName':'avg_amt_last_10m', 'ValueAsString': str(avg_amt_last_10m)},
              {'FeatureName':'num_trans_last_10m', 'ValueAsString': str(num_trans_last_10m)},
              {'FeatureName':'trans_time', 'ValueAsString': str(int(round(time.time())))} #datetime.now().isoformat()} #
             ]
    sm_fs.put_record(FeatureGroupName=fg_name, Record=record)
    return
        
def lambda_handler(event, context):
    
    # Decode Kafka message(s)
    partition0 = event['records']
    records = partition0['ccdesttopic2-0']
    
    #inv_id = event['invocationId']
    #app_arn = event['applicationArn']
    #records = event['records']
    #print(f'Received {len(records)} records, invocation id: {inv_id}, app arn: {app_arn}')
    
    print('Event contains {} records'.format(len(records)))
    
    ret_records = []
    for rec in records:
        
        #data = rec['data']
        #agg_data_str = base64.b64decode(data) 
        #agg_data = json.loads(agg_data_str)

        event_payload_raw_value = base64.b64decode(rec['value'])
        agg_data = json.loads(event_payload_raw_value.decode("utf-8"))
        
        partition = rec['partition']
        offset = rec['offset']

        # cc_num,
        # COUNT(*) OVER LAST_10_MINUTES as cc_count,
        # AVG(amount) OVER LAST_10_MINUTES as avg_amount
        cc_num = agg_data['cc_num']
        num_clicks_last_10m = agg_data['cc_count']
        avg_clicks_last_10m = agg_data['avg_amount']

        print(f' updating agg features for card: {cc_num}, avg amt last 10m: {avg_clicks_last_10m}, num trans last 10m: {num_clicks_last_10m}')
        update_agg(CC_AGG_FEATURE_GROUP, cc_num, avg_clicks_last_10m, num_clicks_last_10m)
        
        # Flag each record as being "Ok", so that Kinesis won't try to re-send 
        ret_records.append({'partition': partition,
                            'offset': offset,
                            'result': 'Ok'})
    return {'records': ret_records}