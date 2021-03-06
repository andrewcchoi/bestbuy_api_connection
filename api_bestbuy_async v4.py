import os
import uuid
import json
import asyncio
import aiohttp
import requests
import pandas as pd

from sqlalchemy import create_engine
from random import uniform
from time import perf_counter, sleep
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from azure.cosmos import exceptions, CosmosClient, PartitionKey

import config_bestbuy


def to_matrix(x, n):
    l = []
    for i in range(x):
        l.append(i+1)
    return [l[i:i+n] for i in range(0, len(l), n)]


async def ceiling_division(n, d):
    return -(n // -d)


async def insert_db(io, engine, db_cols, container, page):

    cols = ['currentPage', 'total', 'totalPages', 'queryTime', 'totalTime', 'canonicalUrl', 
            'sku', 'name', 'type', 'startDate', 'new', 'activeUpdateDate', 'active', 'regularPrice', 
            'salePrice', 'clearance', 'onSale', 'categoryPath', 'customerReviewCount', 'customerReviewAverage', 
            'priceUpdateDate', 'itemUpdateDate', 'class', 'classId', 'subclass', 'subclassId', 'department', 'departmentId', 
            'theatricalReleaseDate', 'studio', 'manufacturer', 'modelNumber', 'condition', 'artistName', 'images', 'image', 'color']
    bool_cols = ["new", "active", "clearance", "onSale"]
    int_cols = ['currentPage', "total", "totalPages"]
    float_cols = ["queryTime", "totalTime", "regularPrice", "salePrice", "customerReviewCount", "customerReviewAverage", 'theatricalReleaseDate']
    date_cols = ["startDate", "activeUpdateDate", "priceUpdateDate", "itemUpdateDate"]
    
    with engine.connect() as cnx:
        # for product in io['products']:
        #     product['currentPage'] = page
        #     product['id'] = str(uuid.uuid4())
        #     container.create_item(body=product)
        df_meta = pd.DataFrame(io)
        df_meta = df_meta.iloc[:, :-1]
        df_products = pd.DataFrame(io['products'])
        df = df_meta.merge(df_products, how='inner', left_index=True, right_index=True)
        df = df.loc[:, cols]
        df.insert(0, 'request_timestamp', datetime.utcnow())

        for col in df.columns.tolist():
            if col in bool_cols:
                df.loc[:, col] = df.loc[:, col].astype('bool')
            elif col in int_cols:
                df.loc[:, col] = df.loc[:, col].astype(int)
            elif col in float_cols:
                df.loc[:, col] = df.loc[:, col].astype(float)
            elif col in date_cols:
                df.loc[:, col] = pd.to_datetime(df.loc[:, col], errors='coerce', infer_datetime_format=True)
            else:
                df.loc[:, col] = df.loc[:, col].astype('str')
                
        df.to_sql(name='products', con=cnx, if_exists='append', index=False)


async def main(api_index=0, folder_index=0, page_size=100, batch_size=5, db=False):

    t0 = perf_counter()
    batch_size = max(1, batch_size)

    folders = ['products', 'categories', 'stores', 'products_update']
    datename = datetime.utcnow().strftime('%Y%m%d')
    cosmos_endpoint = config_bestbuy.bestbuy_cosmosdb_end_point
    cosmos_primary_key = config_bestbuy.bestbuy_cosmosdb_primary_key
    client = CosmosClient(cosmos_endpoint, cosmos_primary_key)
    db_name = 'BestBuyDB'
    database = client.create_database_if_not_exists(id=db_name)
    container_name = 'Products'
    container = database.create_container_if_not_exists(
        id=container_name,
        partition_key=PartitionKey(path='/department'),
        offer_throughput=400
    )
    """"""
    # path = config_bestbuy.path
    path = config_bestbuy.path_test
    foldername = f'best_buy_{datename}\\{folders[folder_index]}'
    folderpath = os.path.join(path, foldername)

    if not os.path.exists(folderpath):
        os.makedirs(folderpath)

    """"""
    # db = os.path.join(config_bestbuy.path, 'bestbuy.db')
    # conn_string = f'sqlite:///{db}'
    # engine = create_engine(conn_string)

    sql_params = quote_plus(config_bestbuy.bestbuy_sql_odbc_string)
    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={sql_params}")

    with engine.connect() as cnx:
        try:
            columns_stmt = "SELECT * FROM products LIMIT 0"
            df_db = pd.read_sql(sql=columns_stmt, con=cnx)
            db_cols = df_db.columns.tolist()

        except Exception as e:
            db_cols = []
            
        try:
            last_update_stmt = 'SELECT MAX(itemUpdateDate) FROM products'
            df_itemUpdateDate = pd.read_sql(sql=last_update_stmt, con=cnx)
            last_update_date = df_itemUpdateDate.iloc[0, 0]

        except Exception as e:
            last_update_date = None

    async def api_bestbuy(session, url, page=1):
        req_params = {
            'apiKey': key, 
            'pageSize': page_size, 
            'format': 'json', 
            'show': 'all',
            'page': page
            }

        while True:
            # delay = round(uniform(0, (batch_size*.4)), 2) # batch size 5 and below???
            delay = round(uniform(0, (await ceiling_division(batch_size, 5)*.4*batch_size)), 1)
            await asyncio.sleep(delay)
            t1 = perf_counter()
            async with session.get(url, params=req_params) as r:
                t2 = perf_counter()
                if r.status != 200:
                    print(f'{page=} | {t1=} | {delay=} | {r.status=}')
                    continue
                
                data = await r.json(content_type=None) # content_type='text/html'
                break

        pg = data.get('currentPage', 0)
        filename = f'best_buy_{datename}_{pg:05}.json'
        filepath = os.path.join(folderpath, filename)

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
        
        if db:
            await insert_db(io=data, engine=engine, db_cols=db_cols, container=container, page=pg)

        t3 = perf_counter()
        print(f'{page=}', f'{t1=}', f'{delay=}', f'{(t2-t1)=}', f'{(t3-t2)=}', f'{(t3-t1)=}', f'{(t3-t0)=}', sep=' | ')

        return data
        
    
    key = config_bestbuy.bestbuy_api_key
    apis = ['products', 'categories', 'stores', f'products(itemUpdateDate>{last_update_date}&active=*)']
    url = f"https://api.bestbuy.com/v1/{apis[api_index]}"

    conn = aiohttp.TCPConnector(limit=10) # default 100, windows limit 64
    async with aiohttp.ClientSession(connector=conn) as session:
        data = await api_bestbuy(session=session, url=url)
        pages = data.get('totalPages', 0)
        # pages = 30
        print(f'{pages=}')
        batches = to_matrix(pages, batch_size)
        for batch in batches:
            tasks = (api_bestbuy(session=session, url=url, page=page) for _, page in enumerate(batch))
            t4 = perf_counter()
            data = await asyncio.gather(*tasks)
            t5 = perf_counter()
            print(f'{batch=} | {t5-t4=} | {t5-t0=}')

            if t5-t4 < 1:
                print('sleeping...')
                await asyncio.sleep(1)
        
    print(f'fin: {perf_counter()-t0=}')
    

if __name__ == '__main__':
    # [0: 'products', 1: 'categories', 2: 'stores', 3: f'products(itemUpdateDate>{last_update_date}&active=*)']
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(api_index=0, folder_index=0, page_size=100, batch_size=5, db=False))
    # loop.close()

"""
https://api.bestbuy.com/v1/products?apiKey={}&pageSize=100&format=json&show=all&page=1

resources:
https://docs.aiohttp.org/en/stable/http_request_lifecycle.html
https://docs.aiohttp.org/en/stable/client_reference.html#tcpconnector
"""
