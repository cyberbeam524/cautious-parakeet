from pyspark.sql.types import (StructField, StructType, StringType, DoubleType, 
                               TimestampType, LongType)
from pyspark.sql.functions import desc, dense_rank, col, when, count, avg
from pyspark.sql import SparkSession, DataFrame
from pyspark.sql.window import Window
from argparse import ArgumentParser, Namespace
import logging
import boto3
import time
import sys
import os

# Total unique users expected (for validation)
TOTAL_UNIQUE_USERS = 10000
# Replace with your actual feature group name for batch aggregated user behavior features
FEATURE_GROUP = 'user_behavior_batch_fg_name'

logger = logging.getLogger('sagemaker')
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

feature_store_client = boto3.client(service_name='sagemaker-featurestore-runtime')

def parse_args() -> Namespace:
    parser = ArgumentParser(description='Spark Job Input and Output Args for User Behavior Aggregation')
    parser.add_argument('--s3_input_bucket', type=str, help='S3 Input Bucket')
    parser.add_argument('--s3_input_key_prefix', type=str, help='S3 Input Key Prefix')
    parser.add_argument('--s3_output_bucket', type=str, help='S3 Output Bucket')
    parser.add_argument('--s3_output_key_prefix', type=str, help='S3 Output Key Prefix')
    args = parser.parse_args()
    return args

def define_schema() -> StructType:
    # Define schema based on your sample CSV:
    schema = StructType([
        StructField('index', StringType(), True),  # if present as the first column
        StructField('event_id', StringType(), True),
        StructField('timestamp', TimestampType(), True),
        StructField('customer_id', LongType(), True),
        StructField('session_id', StringType(), True),
        StructField('event_type', StringType(), True),
        StructField('product_id', DoubleType(), True),
        StructField('product_category', StringType(), True),
        StructField('price', DoubleType(), True),
        StructField('order_in_session', LongType(), True),
        # These fields may be null if not applicable; adjust types as needed:
        StructField('purchased_items', LongType(), True),
        StructField('total_amount', DoubleType(), True),
        StructField('interaction_value', LongType(), True),
        StructField('cumsum_interactions', LongType(), True)
    ])
    return schema

def aggregate_features(args: Namespace, schema: StructType, spark: SparkSession) -> DataFrame:
    logger.info('[Read User Behavior Data as Spark DataFrame]')
    input_path = f's3a://{os.path.join(args.s3_input_bucket, args.s3_input_key_prefix)}'
    events_df = spark.read.csv(input_path, header=True, schema=schema)
    
    logger.info('[Filter and Aggregate Order Data]')
    # We assume that order events are indicated by event_type = 'order'
    # If your data marks orders in another way (e.g., non-null total_amount), adjust the filter accordingly.
    orders_df = events_df.filter(col('event_type') == 'order')
    
    # Define windows:
    # For batch aggregates, we compute aggregates over the past 1 week.
    window_1w = Window.partitionBy('customer_id')\
                      .orderBy(col('timestamp').cast("long"))\
                      .rangeBetween(-7 * 24 * 3600, 0)
    
    aggregated_df = orders_df.withColumn('total_orders_last_1w', count('*').over(window_1w)) \
                              .withColumn('avg_order_value_last_1w', avg(col('total_amount')).over(window_1w))
    
    # Optionally, you can compute additional features. For example, if you want to include a real-time like metric
    # computed over a shorter interval (e.g., last 5 minutes) for clicks:
    clicks_df = events_df.filter(col('event_type') == 'click')
    window_5m = Window.partitionBy('customer_id')\
                      .orderBy(col('timestamp').cast("long"))\
                      .rangeBetween(-5 * 60, 0)
    clicks_agg = clicks_df.withColumn('clicks_last_5m', count('*').over(window_5m))
    
    # Join the aggregates by customer_id. Depending on your needs, you may join batch and streaming features,
    # or process them in separate feature groups.
    agg_joined = aggregated_df.join(clicks_agg.select('customer_id', 'clicks_last_5m'),
                                    on='customer_id', how='left')
    
    # Remove duplicates by taking the latest record per customer.
    window_latest = Window.partitionBy('customer_id').orderBy(desc('timestamp'))
    sorted_df = agg_joined.withColumn('rank', dense_rank().over(window_latest))
    grouped_df = sorted_df.filter(col('rank') == 1).drop('rank')
    
    # Select the fields that match your feature group schema
    # Here, we assume the batch feature group expects: user_id, total_orders_last_1w, avg_order_value_last_1w, clicks_last_5m, event_time
    final_df = grouped_df.select(
        col('customer_id').alias('user_id'),
        col('total_orders_last_1w'),
        col('avg_order_value_last_1w'),
        col('clicks_last_5m'),
        col('timestamp').alias('event_time')
    )
    return final_df

def write_to_s3(args: Namespace, aggregated_features: DataFrame) -> None:
    logger.info('[Write Aggregated Features to S3]')
    output_path = 's3a://' + os.path.join(args.s3_output_bucket, args.s3_output_key_prefix)
    aggregated_features.coalesce(1) \
                       .write.format('csv') \
                       .option('header', True) \
                       .mode('overwrite') \
                       .save(output_path)
    
def group_by_user(aggregated_features: DataFrame) -> DataFrame: 
    logger.info('[Group Aggregated Features by User]')
    window = Window.partitionBy('user_id').orderBy(desc('event_time'))
    sorted_df = aggregated_features.withColumn('rank', dense_rank().over(window))
    grouped_df = sorted_df.filter(col('rank') == 1).drop('rank')
    # Select only the columns to be ingested into the feature store
    sliced_df = grouped_df.select('user_id', 'total_orders_last_1w', 'avg_order_value_last_1w', 'clicks_last_5m')
    return sliced_df

def transform_row(sliced_df: DataFrame) -> list:
    logger.info('[Transform Spark DataFrame Rows to Feature Store Records]')
    records = []
    for row in sliced_df.rdd.collect():
        record = []
        user_id, total_orders_last_1w, avg_order_value_last_1w, clicks_last_5m = row
        if user_id is not None:
            record.append({'FeatureName': 'user_id', 'ValueAsString': str(user_id)})
            record.append({'FeatureName': 'total_orders_last_1w', 'ValueAsString': str(total_orders_last_1w)})
            record.append({'FeatureName': 'avg_order_value_last_1w', 'ValueAsString': str(round(avg_order_value_last_1w, 2) if avg_order_value_last_1w else 0.0)})
            record.append({'FeatureName': 'clicks_last_5m', 'ValueAsString': str(clicks_last_5m if clicks_last_5m else 0)})
            records.append(record)
    return records

def write_to_feature_store(records: list) -> None:
    logger.info('[Write Grouped Features to SageMaker Feature Store]')
    success, fail = 0, 0
    for record in records:
        event_time_feature = {
                'FeatureName': 'event_time',
                'ValueAsString': str(int(round(time.time())))
            }
        record.append(event_time_feature)
        response = feature_store_client.put_record(FeatureGroupName=FEATURE_GROUP, Record=record)
        if response['ResponseMetadata']['HTTPStatusCode'] == 200:
            success += 1
        else:
            fail += 1
    logger.info('Success = {}'.format(success))
    logger.info('Fail = {}'.format(fail))
    # You can adjust these assertions based on your expected unique user count
    assert success <= TOTAL_UNIQUE_USERS
    assert fail == 0

def run_spark_job():
    spark = SparkSession.builder.appName('UserBehaviorBatchAggregationJob').getOrCreate()
    args = parse_args()
    schema = define_schema()
    aggregated_features = aggregate_features(args, schema, spark)
    write_to_s3(args, aggregated_features)
    grouped_features = group_by_user(aggregated_features)
    records = transform_row(grouped_features)
    write_to_feature_store(records)
    
if __name__ == '__main__':
    run_spark_job()
