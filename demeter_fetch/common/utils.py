import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from demeter_fetch import constants
from demeter_fetch.common._typing import *


def get_file_name(chain: ChainType, pool_address, day: date | str):
    if isinstance(day, date):
        return f"{chain.name}-{pool_address}-{day.strftime('%Y-%m-%d')}.raw.csv"
    else:
        return f"{chain.name}-{pool_address}-{day}.raw.csv"


def get_aave_file_name(chain: ChainType, token_address, day: date):
    return f"{chain.name}-aave_v3-{token_address}-{day.strftime('%Y-%m-%d')}.raw.csv"


def convert_raw_file_name(file: str, to_config: ToConfig) -> str:
    file_name = os.path.basename(file)
    file_name_and_ext = os.path.splitext(file_name)

    return os.path.join(
        to_config.save_path,
        f"{file_name_and_ext[0].replace('.raw', '')}.{to_config.type.name}{file_name_and_ext[1]}",
    )


def print_log(*args):
    new_tuple = (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)
    new_tuple = new_tuple + args
    print(*new_tuple)




class TextUtil(object):
    @staticmethod
    def cut_after(text: str, symbol: str) -> str:
        index = text.find(symbol)
        return text[0:index]


class TimeUtil(object):
    @staticmethod
    def get_minute(time: datetime) -> datetime:
        return datetime(time.year, time.month, time.day, time.hour, time.minute, 0)

    @staticmethod
    def get_date_array(date_begin, date_end) -> List[date]:
        return [date_begin + timedelta(days=x) for x in range(0, 1 + (date_end - date_begin).days)]


class HexUtil(object):
    @staticmethod
    def to_signed_int(h):
        """
        Converts hex values to signed integers.
        """
        s = bytes.fromhex(h[2:])
        i = int.from_bytes(s, "big", signed=True)
        return i


class DataUtil(object):
    @staticmethod
    def fill_missing(data_list: List[MinuteData]) -> List[MinuteData]:
        if len(data_list) < 1:
            return data_list
        # take the first minute in data. instead of 0:00:00
        # so here will be a problem, if the first data is 0:03:00, the first 2 minutes will be blank
        # that's because there is no previous data to follow
        # those empty rows will be filled in loading stage
        index_minute = data_list[0].timestamp
        new_list = []
        data_list_index = 0

        start_day = data_list[0].timestamp.day
        while index_minute.day == start_day:
            if (data_list_index < len(data_list)) and (index_minute == data_list[data_list_index].timestamp):
                item = data_list[data_list_index]
                data_list_index += 1
            else:
                item = MinuteData()
                item.timestamp = index_minute
            prev_data = new_list[len(new_list) - 1] if len(new_list) - 1 >= 0 else None
            # if no previous(this might happen in the first minutes) data, this row will be discarded
            if item.fill_missing_field(prev_data):
                new_list.append(item)
            index_minute = index_minute + timedelta(minutes=1)

        return new_list


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(obj, date):
            return obj.strftime("%Y-%m-%d")
        else:
            return json.JSONEncoder.default(self, obj)


def hex_to_length(hex_str: str, new_length: int):
    """
    expend hex string to new length, will keep leading 0x if it exists
    eg:
    1. hex_str=0x1, length=3, result is 0x001
    2. hex_str=0x001, length=2, result is 0x01
    """

    has_0x = hex_str[0:2] == "0x" if len(hex_str) >= 2 else False

    hex_without_0x = hex_str if not has_0x else hex_str[2:]

    if len(hex_without_0x) <= new_length:
        return hex_without_0x.zfill(new_length) if not has_0x else "0x" + hex_without_0x.zfill(new_length)
    else:
        if hex_without_0x[-new_length - 1] != "0":
            raise RuntimeError("Not enough leading zeros to remove")
        return hex_without_0x[-new_length:] if not has_0x else "0x" + hex_without_0x[-new_length:]


class ApiUtil:
    @staticmethod
    def query_blockno_from_time(chain: ChainType, blk_time: datetime, is_before: bool = True, proxy="",
                                etherscan_api_key=None):
        proxies = (
            {
                "http": proxy,
                "https": proxy,
            }
            if proxy
            else {}
        )
        before_or_after = "before" if is_before else "after"
        url = ChainTypeConfig[chain]["query_height_api"]
        blk_time = blk_time.replace(tzinfo=timezone.utc)
        url = url.replace("%1", str(int(blk_time.timestamp()))).replace("%2", before_or_after)
        if etherscan_api_key is not None:
            url += "&apikey=" + etherscan_api_key
        retry = 3
        result = None
        while retry:
            try:
                result = requests.get(url, proxies=proxies)
                retry = 0
            except Exception as ex:
                retry -= 1
        if not result:
            raise RuntimeError("request error with retry 3 times.")
        if result.status_code != 200:
            raise RuntimeError("request block number failed, code: " + str(result.status_code))
        result_json = result.json()
        if int(result_json["status"]) == 1:
            return int(result_json["result"])
        else:
            raise RuntimeError("request block number failed, message: " + str(result_json))


class UniswapUtil:
    @staticmethod
    def add_proxy_log(df, index, proxy_row):
        df.loc[index, "proxy_data"] = proxy_row.data
        df.at[index, "proxy_topics"] = proxy_row.topics
        df.loc[index, "proxy_log_index"] = proxy_row.log_index

    @staticmethod
    def match_proxy_log(pool_logs: pd.DataFrame, proxy_logs: pd.DataFrame):
        pool_logs["proxy_topics"] = [[]] * pool_logs.shape[0]

        for index, row in pool_logs.iterrows():
            if row.tx_type == OnchainTxType.SWAP:
                continue
            if row.transaction_hash not in proxy_logs.index:
                continue
            proxy_tx: pd.DataFrame = proxy_logs.loc[[row.transaction_hash]]
            proxy_tx_matched: pd.DataFrame = proxy_tx.loc[proxy_tx.topic_name == constants.topic_dict[row.topic_name]]

            for pindex, possible_match in proxy_tx_matched.iterrows():
                if row.tx_type == OnchainTxType.MINT:
                    if row.pool_data[66:] == possible_match.data[2:]:
                        UniswapUtil.add_proxy_log(pool_logs, index, possible_match)
                        break
                elif row.tx_type == OnchainTxType.COLLECT or row.tx_type == OnchainTxType.BURN:
                    if UniswapUtil.compare_burn_data(row.pool_data, possible_match.data):
                        UniswapUtil.add_proxy_log(pool_logs, index, possible_match)
                        break
                else:
                    raise ValueError("not support tx type")
        # if no column is generated

        if "proxy_data" not in pool_logs.columns:
            pool_logs["proxy_data"] = None
            pool_logs["proxy_topics"] = [[]] * pool_logs.shape[0]
            pool_logs["proxy_log_index"] = None
        else:
            pool_logs["proxy_topics"] = pool_logs["proxy_topics"].fillna("[]")

    @staticmethod
    def compare_int_with_error(a: int, b: int, error: int = None) -> bool:
        if error is None:
            if a > 10 ** 10:
                error = 25
            elif a > 10 ** 6:
                error = 10
            elif a > 10 ** 2:
                error = 3
            else:
                error = 1
        return abs(a - b) <= error

    @staticmethod
    def compare_burn_data(a: str, b: str) -> bool:
        """
        0x0000000000000000000000000000000000000000000000000014aca30ddf7569
          000000000000000000000000000000000000000000000000000041b051acc70d
          0000000000000000000000000000000000000000000000000000000000000000

        """
        if len(a) != 194 or len(b) != 194:
            return False
        if a[0:66] != b[0:66]:
            return False
        if not UniswapUtil.compare_int_with_error(int("0x" + a[66: 66 + 64], 16), int("0x" + b[66: 66 + 64], 16)):
            return False
        if not UniswapUtil.compare_int_with_error(int("0x" + a[66 + 64: 66 + 2 * 64], 16),
                                                  int("0x" + b[66 + 64: 66 + 2 * 64], 16)):
            return False
        return True