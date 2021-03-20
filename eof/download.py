#!/usr/bin/env python
"""
Utility for downloading Sentinel precise orbit ephemerides (EOF) files

Example filtering URL:
?validity_start_time=2014-08&page=2

Example EOF: 'S1A_OPER_AUX_POEORB_OPOD_20140828T122040_V20140806T225944_20140808T005944.EOF'

 'S1A' : mission id (satellite it applies to)
 'OPER' : OPER for "Routine Operations" file
 'AUX_POEORB' : AUX_ for "auxiliary data file", POEORB=Precise Orbit Ephemerides (POE) Orbit File
 'OPOD'  Site Center of the file originator

 '20140828T122040' creation date of file
 'V20140806T225944' Validity start time (when orbit is valid)
 '20140808T005944' Validity end time

Full EOF sentinel doumentation:
https://earth.esa.int/documents/247904/349490/GMES_Sentinels_POD_Service_File_Format_Specification_GMES-GSEG-EOPG-FS-10-0075_Issue1-3.pdf

API documentation: https://qc.sentinel1.eo.esa.int/doc/api/

See parsers for Sentinel file naming description
"""
import os
import glob
import itertools
import requests
from multiprocessing.pool import ThreadPool
from datetime import timedelta, datetime
from dateutil.parser import parse
from .parsing import EOFLinkFinder
from .products import Sentinel, SentinelOrbit
from .log import logger

MAX_WORKERS = 20  # For parallel downloading

BASE_URL = "http://aux.sentinel1.eo.esa.int/{orbit_type}/{dt}/"
DT_FMT = "%Y/%m/%d"
# e.g. "http://aux.sentinel1.eo.esa.int/POEORB/2021/03/18/"
# This page has links with relative urls in the <a> tags, such as:
# S1A_OPER_AUX_POEORB_OPOD_20210318T121438_V20210225T225942_20210227T005942.EOF

PRECISE_ORBIT = "POEORB"
RESTITUTED_ORBIT = "RESORB"


def download_eofs(orbit_dts=None, missions=None, sentinel_file=None, save_dir="."):
    """Downloads and saves EOF files for specific dates

    Args:
        orbit_dts (list[str] or list[datetime.datetime]): datetime for orbit coverage
        missions (list[str]): optional, to specify S1A or S1B
            No input downloads both, must be same len as orbit_dts
        sentinel_file (str): path to Sentinel-1 filename to download one .EOF for
        save_dir (str): directory to save the EOF files into

    Returns:
        list[str]: all filenames of saved orbit files

    Raises:
        ValueError - for missions argument not being one of 'S1A', 'S1B',
            having different lengths, or `sentinel_file` being invalid
    """
    # TODO: condense list of same dates, different hours?
    if missions and all(m not in ("S1A", "S1B") for m in missions):
        raise ValueError('missions argument must be "S1A" or "S1B"')
    if sentinel_file:
        sent = Sentinel(sentinel_file)
        orbit_dts, missions = [sent.start_time], [sent.mission]
    if missions and len(missions) != len(orbit_dts):
        raise ValueError("missions arg must be same length as orbit_dts")
    if not missions:
        missions = itertools.repeat(None)

    # First make sures all are datetimes if given string
    orbit_dts = [parse(dt) if isinstance(dt, str) else dt for dt in orbit_dts]

    # Download and save all links in parallel
    pool = ThreadPool(processes=MAX_WORKERS)
    result_dt_dict = {
        pool.apply_async(_download_and_write, (mission, dt, save_dir)): dt
        for mission, dt in zip(missions, orbit_dts)
    }
    filenames = []
    for result in result_dt_dict:
        cur_filenames = result.get()
        dt = result_dt_dict[result]
        logger.info("Finished {}, saved to {}".format(dt.date(), cur_filenames))
        filenames.extend(cur_filenames)
    return filenames


def eof_list(start_dt, mission, orbit_type=PRECISE_ORBIT):
    """Download the list of .EOF files for a specific date

    Args:
        start_dt (str or datetime): Year month day of validity start for orbit file

    Returns:
        list: urls of EOF files

    Raises:
        ValueError: if start_dt returns no results

    Usage:
    >>> from datetime import datetime
    >>> eof_list(datetime(2021, 2, 18), "S1A")
    (['http://aux.sentinel1.eo.esa.int/POEORB/2021/03/10/\
S1A_OPER_AUX_POEORB_OPOD_20210310T121945_V20210217T225942_20210219T005942.EOF'], 'POEORB')
    """
    # Unfortunately, the archive page stores the file based on "creation date", but we
    # care about the "validity date"
    # TODO: take this out once the new ESA API is up.
    if orbit_type == PRECISE_ORBIT:
        # ESA seems to reliably upload the POEORB files 3 weeks after the flyover
        validity_creation_diff = timedelta(days=20, hours=12)
    else:
        validity_creation_diff = timedelta(hours=4)
    search_dt = start_dt + validity_creation_diff
    url = BASE_URL.format(orbit_type=orbit_type, dt=search_dt.strftime(DT_FMT))

    logger.info("Searching for EOFs at {}".format(url))
    response = requests.get(url)
    if response.status_code == 404:
        if orbit_type == PRECISE_ORBIT:
            logger.warning(
                "Precise orbits not avilable yet for {}, trying RESORB".format(
                    search_dt
                )
            )
            return eof_list(start_dt, mission, orbit_type=RESTITUTED_ORBIT)
        else:
            raise ValueError("Orbits not avilable yet for {}".format(search_dt))
    # Check for any other problem
    response.raise_for_status()

    parser = EOFLinkFinder()
    parser.feed(response.text)
    # Append the test url, since the links on the page are relative (don't contain full url)
    links = [url + link for link in parser.eof_links if link.startswith(mission)]

    if len(links) < 1:
        if orbit_type == PRECISE_ORBIT:
            logger.warning(
                "No precise orbit files found for {} on {}, searching RESORB".format(
                    mission, start_dt.strftime(DT_FMT)
                )
            )
            return eof_list(start_dt, mission, orbit_type=RESTITUTED_ORBIT)

        raise ValueError(
            "No EOF files found for {} on {} at {}".format(
                start_dt.strftime(DT_FMT), mission, url
            )
        )
    return links, orbit_type


def _dedupe_links(links):
    out = [links[0]]
    orb1 = SentinelOrbit(links[0].split("/")[-1])
    for link in links[1:]:
        if SentinelOrbit(link.split("/")[-1]).date != orb1.date:
            out.append(link)
    return out


def _pick_precise_file(links, sent_date):
    """Choose the precise file with (sent_date - 1, sent_date + 1)"""
    out = []
    for link in links:
        so = SentinelOrbit(link.split("/")[-1])
        # hotfix until I figure out what the RAW processor is doing with the orbtimings
        if (so.start_time.date() == (sent_date - timedelta(days=1)).date()) and (
            so.stop_time.date() == (sent_date + timedelta(days=1)).date()
        ):
            out.append(link)
    return out


def _download_and_write(mission, dt, save_dir="."):
    """Wrapper function to run the link downloading in parallel

    Args:
        mission (str): Sentinel mission: either S1A or S1B
        dt (datetime): datetime of Sentinel product
        save_dir (str): directory to save the EOF files into

    Returns:
        list[str]: Filenames to which the orbit files have been saved
    """
    try:
        cur_links, orbit_type = eof_list(dt, mission)
    except ValueError as e:  # 0 found for date
        logger.warning(e.args[0])
        logger.warning("Skipping {}".format(dt.strftime("%Y-%m-%d")))
        return

    cur_links = _dedupe_links(cur_links)
    if orbit_type == PRECISE_ORBIT:
        cur_links = _pick_precise_file(cur_links, dt)

    # RESORB has multiple overlapping
    saved_files = []
    for link in cur_links:
        fname = os.path.join(save_dir, link.split("/")[-1])
        if os.path.isfile(fname):
            logger.info("%s already exists, skipping download.", link)
            # TODO: If I return here.. do I ever want to iterate
            # and save multiple links?
            return [fname]

        logger.info("Downloading %s", link)
        response = requests.get(link)
        response.raise_for_status()
        logger.info("Saving to %s", fname)
        with open(fname, "wb") as f:
            f.write(response.content)
        saved_files.append(fname)
    return saved_files


def find_current_eofs(cur_path):
    """Returns a list of SentinelOrbit objects located in `cur_path`"""
    return sorted(
        [
            SentinelOrbit(filename)
            for filename in glob.glob(os.path.join(cur_path, "*EOF"))
        ]
    )


def find_unique_safes(search_path):
    file_set = set()
    for filename in glob.glob(os.path.join(search_path, "S1*")):
        try:
            parsed_file = Sentinel(filename)
        except ValueError:  # Doesn't match a sentinel file
            logger.debug("Skipping {}, not a Sentinel 1 file".format(filename))
            continue
        file_set.add(parsed_file)
    return file_set


def find_scenes_to_download(search_path="./", save_dir="./"):
    """Parse the search_path directory for any Sentinel 1 products' date and mission"""
    orbit_dts = []
    missions = []
    # Check for already-downloaded orbit files, skip ones we have
    current_eofs = find_current_eofs(save_dir)

    # Now loop through each Sentinel scene in search_path
    for parsed_file in find_unique_safes(search_path):

        if (
            parsed_file.start_time in orbit_dts
        ):  # start_time is a datetime, already found
            continue
        if any(parsed_file.start_time in orbit for orbit in current_eofs):
            logger.info(
                "Skipping {}, already have EOF file".format(
                    os.path.splitext(parsed_file.filename)[0]
                )
            )
            continue

        logger.info(
            "Downloading precise orbits for {} on {}".format(
                parsed_file.mission, parsed_file.start_time.strftime("%Y-%m-%d")
            )
        )
        orbit_dts.append(parsed_file.start_time)
        missions.append(parsed_file.mission)

    return orbit_dts, missions


def main(search_path=".", save_dir=",", sentinel_file=None, mission=None, date=None):
    """Function used for entry point to download eofs"""

    if not os.path.exists(save_dir):
        logger.info("Creating directory for output: %s", save_dir)
        os.mkdir(save_dir)

    if (mission and not date) or (date and not mission):
        raise ValueError("Must specify date and mission together")

    if sentinel_file:
        # Handle parsing in download_eof
        orbit_dts, missions = None, None
    elif date:
        orbit_dts = [date]
        missions = [mission]
    else:
        # No command line args given: search current directory
        orbit_dts, missions = find_scenes_to_download(
            search_path=search_path, save_dir=save_dir
        )
        if not orbit_dts:
            logger.info(
                "No Sentinel products found in directory %s, exiting", search_path
            )
            return 0

    return download_eofs(
        orbit_dts=orbit_dts,
        missions=missions,
        sentinel_file=sentinel_file,
        save_dir=save_dir,
    )


def catch(sensor="S1A", osvtype=PRECISE_ORBIT, start=None, stop=None, timeout=20):
    """
    check a server for files
    Parameters
    ----------
    sensor: str or list
        The S1 mission(s):
            - 'S1A'
            - 'S1B'
            - ['S1A', 'S1B']
    osvtype: {'POEORB', 'RESORB'}
        Type of orbit (precise or restituted) to search for
    start: str
        the date to start searching for files in format YYYYmmddTHHMMSS
    stop: str
        the date to stop searching for files in format YYYYmmddTHHMMSS
    Returns
    -------
    list
        the product dictionary of the remote OSV files, with href
    """
    url = "https://scihub.copernicus.eu/gnss/search/"
    auth = ("gnssguest", "gnssguest")
    # a dictionary for storing the url arguments
    query = {}

    if osvtype == PRECISE_ORBIT:
        query["producttype"] = "AUX_POEORB"
    elif osvtype == RESTITUTED_ORBIT:
        query["producttype"] = "AUX_RESORB"
    else:
        raise ValueError("osvtype must be either 'POE' or 'RES'")

    if sensor in ["S1A", "S1B"]:
        query["platformname"] = "Sentinel-1"
        # filename starts w/ sensor
        query["filename"] = "{}*".format(sensor)
    elif sorted(sensor) == ["S1A", "S1B"]:
        query["platformname"] = "Sentinel-1"
    else:
        raise RuntimeError("unsupported input for parameter sensor")

    # the collection of files to be returned
    collection = []
    # set the defined date or the date of the first existing OSV file otherwise
    # two days are added/subtracted from the defined start and stop dates since the
    # online query does only allow for searching the start time; hence, if e.g.
    # the start date is 2018-01-01T000000, the query would not return the corresponding
    # file, whose start date is 2017-12-31 (V20171231T225942_20180102T005942)
    if start is not None:
        date_start = parse(start).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        date_start = datetime.strptime("2014-07-31", "%Y-%m-%d").strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    # set the defined date or the current date otherwise
    if stop is not None:
        date_stop = parse(stop).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        date_stop = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    # append the time frame to the query dictionary
    query["beginPosition"] = "[{} TO {}]".format(date_start, date_stop)
    query["endPosition"] = "[{} TO {}]".format(date_start, date_stop)
    import ipdb

    ipdb.set_trace()
    print("searching for new {} files".format(osvtype))
    query_list = []
    for keyword, value in query.items():
        query_elem = "{}:{}".format(keyword, value)
        query_list.append(query_elem)
    query_str = " ".join(query_list)
    target = "{}?q={}&format=json".format(url, query_str)
    print(target)

    def _parse_gnsssearch_json(search_dict):
        parsed_dict = {}
        # Will return ['entry'] as dict if only one item
        # If so just make a list
        if isinstance(search_dict, dict):
            search_dict = [search_dict]
        for entry in search_dict:
            id = entry["id"]
            entry_dict = {}

            for key, value in entry.items():
                if key == "title":
                    entry_dict[key] = value
                elif key == "id":
                    entry_dict[key] = value
                elif key == "ondemand":
                    if value.lower() == "true":
                        entry_dict[key] = True
                    else:
                        entry_dict[key] = False
                elif key == "str":
                    for elem in value:
                        entry_dict[elem["name"]] = elem["content"]
                elif key == "link":
                    for elem in value:
                        if "rel" in elem.keys():
                            href_key = "href_" + elem["rel"]
                            entry_dict[href_key] = elem["href"]
                        else:
                            entry_dict["href"] = elem["href"]
                elif key == "date":
                    for elem in value:
                        entry_dict[elem["name"]] = parse(elem["content"])

            parsed_dict[id] = entry_dict
        return parsed_dict

    def _parse_gnsssearch_response(response_json):
        if "entry" in response_json.keys():
            search_dict = response_json["entry"]
            parsed_dict = _parse_gnsssearch_json(search_dict)
        else:
            parsed_dict = {}
        return parsed_dict

    response = requests.get(target, auth=auth, timeout=timeout)
    response.raise_for_status()
    response_json = response.json()["feed"]
    total_results = response_json["opensearch:totalResults"]
    print("found {} OSV results".format(total_results))
    subquery = [
        link["href"] for link in response_json["link"] if link["rel"] == "self"
    ][0]
    if int(total_results) > 10:
        subquery = subquery.replace("rows=10", "rows=100")

    while subquery:
        subquery_response = requests.get(subquery, auth=auth, timeout=timeout)
        subquery_response.raise_for_status()
        subquery_json = subquery_response.json()["feed"]
        subquery_products = _parse_gnsssearch_response(subquery_json)
        collection += list(subquery_products.values())
        if "next" in [link["rel"] for link in subquery_json["link"]]:
            subquery = [
                link["href"] for link in subquery_json["link"] if link["rel"] == "next"
            ][0]
        else:
            subquery = None
    # if osvtype == "RES" and maxdate("POE", "stop") is not None:
    #     collection = [
    #         x
    #         for x in collection
    #         if self.date(x["filename"], "start") > self.maxdate("POE", "stop")
    #     ]
    return collection
