"""sentinelsat based client to get orbit files form scihub.copernicu.eu."""

import logging
import requests
import datetime
import operator
from typing import Sequence

from .products import SentinelOrbit, Sentinel as S1Product

from sentinelsat import SentinelAPI
from sentinelsat.exceptions import ServerError


_log = logging.getLogger(__name__)


class ValidityError(ValueError):
    pass


def get_validity_info(products: Sequence[str]) -> Sequence[SentinelOrbit]:
    return [SentinelOrbit(product_id) for product_id in products]


def lastval_cover(
    t0: datetime.datetime, t1: datetime.datetime, data: Sequence[SentinelOrbit]
) -> str:
    candidates = [
        item for item in data if item.start_time <= t0 and item.stop_time >= t1
    ]
    if not candidates:
        raise ValidityError(
            f"none of the input products completely covers the requested "
            f"time interval: [t0={t0}, t1={t1}]"
        )

    candidates.sort(key=operator.attrgetter("created_time"), reverse=True)

    return candidates[0].filename


class OrbitSelectionError(RuntimeError):
    pass


class ScihubGnssClient:
    T0 = datetime.timedelta(days=1)
    T1 = datetime.timedelta(days=1)

    def __init__(
        self,
        user: str = "gnssguest",
        password: str = "gnssguest",
        api_url: str = "https://scihub.copernicus.eu/gnss/",
        **kwargs,
    ):
        self._api = SentinelAPI(user=user, password=password, api_url=api_url, **kwargs)

    def query_orbit(self, t0, t1, satellite_id: str, product_type: str = "AUX_POEORB"):
        assert satellite_id in {"S1A", "S1B"}
        assert product_type in {"AUX_POEORB", "AUX_RESORB"}

        query_params = dict(
            producttype=product_type,
            platformserialidentifier=satellite_id[1:],
            # https://github.com/sentinelsat/sentinelsat/issues/551#issuecomment-992344180
            # date=[t0, t1],
            beginposition=(None, t1),
            endposition=(t0, None),
        )
        _log.debug("query parameter: %s", query_params)
        products = self._api.query(**query_params)
        return products

    @staticmethod
    def _select_orbit(products, t0, t1):
        if not products:
            return {}
        orbit_products = [p["identifier"] for p in products.values()]
        validity_info = get_validity_info(orbit_products)
        product_id = lastval_cover(t0, t1, validity_info)
        return {k: v for k, v in products.items() if v["identifier"] == product_id}

    def query_orbit_for_product(
        self,
        product,
        orbit_type: str = "precise",
        t0_margin: datetime.timedelta = T0,
        t1_margin: datetime.timedelta = T1,
    ):
        if isinstance(product, str):
            product = S1Product(product)

        return self.query_orbit_by_dt(
            [product.mission],
            [product.start_time],
            orbit_type=orbit_type,
            t0_margin=t0_margin,
            t1_margin=t1_margin,
        )

    def query_orbit_by_dt(
        self,
        missions,
        orbit_dts,
        orbit_type: str = "precise",
        t0_margin: datetime.timedelta = T0,
        t1_margin: datetime.timedelta = T1,
    ):
        """Query the Scihub api for product info for the specified missions/orbit_dts.

        Args:
            missions (list[str]): list of mission names
            orbit_dts (list[datetime.datetime]): list of orbit datetimes
            orbit_type (str, optional): Type of orbit to prefer in search. Defaults to "precise".
            t0_margin (datetime.timedelta, optional): Margin used in searching for early bound
                for orbit.  Defaults to 1 day.
            t1_margin (datetime.timedelta, optional): Margin used in searching for late bound
                for orbit.  Defaults to 1 day.

        Returns:
            query (dict): API info from scihub with the requested products
        """
        remaining_dates = []
        query = {}
        for mission, dt in zip(missions, orbit_dts):
            found_result = False
            # Only check for previse orbits if that is what we want
            if orbit_type == "precise":
                products = self.query_orbit(
                    dt - t0_margin,
                    dt + t1_margin,
                    mission,
                    product_type="AUX_POEORB",
                )
                result = (
                    self._select_orbit(products, dt, dt + datetime.timedelta(minutes=1))
                    if products
                    else None
                )
            else:
                result = None

            if result:
                found_result = True
                query.update(result)
            else:
                # try with RESORB
                products = self.query_orbit(
                    dt - datetime.timedelta(hours=1),
                    dt + datetime.timedelta(hours=1),
                    mission,
                    product_type="AUX_RESORB",
                )
                result = (
                    self._select_orbit(products, dt, dt + datetime.timedelta(minutes=1))
                    if products
                    else None
                )
                if result:
                    found_result = True
                    query.update(result)

            if not found_result:
                remaining_dates.append((mission, dt))

        if remaining_dates:
            _log.warning("The following dates were not found: %s", remaining_dates)
        return query

    def download(self, uuid, **kwargs):
        """Download a single orbit product.

        See sentinelsat.SentinelAPI.download for a detailed description
        of arguments.
        """
        return self._api.download(uuid, **kwargs)

    def download_all(self, products, **kwargs):
        """Download all the specified orbit products.

        See sentinelsat.SentinelAPI.download_all for a detailed description
        of arguments.
        """
        return self._api.download_all(products, **kwargs)

    def server_is_up(self):
        """Ping the ESA server using sentinelsat to verify the connection."""
        try:
            self._api.query(producttype="AUX_POEORB", platformserialidentifier="S1A")
            return True
        except ServerError as e:
            _log.warning("Cannot connect to the server: %s", e)
            return False


class ASFClient:
    url = "https://s1qc.asf.alaska.edu/aux_poeorb/"

    def get_eof_list(self, dt):
        from .parsing import EOFLinkFinder

        resp = requests.get(self.url)
        finder = EOFLinkFinder()
        finder.feed(resp.text)
        return [SentinelOrbit(f) for f in finder.eof_links]

    def get_download_url(self, dt):
        filename = lastval_cover(dt, dt, self.get_eof_list(dt))

    # def _find_straddling_orbit(test_dt, all_eofs):
    #     straddling = [orb for orb in all_eofs if  orb.start_time.date() < test_dt.date() < orb.stop_time.date()]
    #     if len(straddling) == 0:
    #         raise ValueError("No matching orbit found for {}".format(str(test_dt)))
    #     return straddling


# In [45]: [ff.filename for ff in _find_straddling_orbit(test, sobs)]
# Out[45]:
# ['S1B_OPER_AUX_POEORB_OPOD_20200909T111317_V20200819T225942_20200821T005942.EOF',
#  'S1A_OPER_AUX_POEORB_OPOD_20200909T121359_V20200819T225942_20200821T005942.EOF',
#  'S1A_OPER_AUX_POEORB_OPOD_20210317T080741_V20200819T225942_20200821T005942.EOF',
#  'S1B_OPER_AUX_POEORB_OPOD_20210317T064752_V20200819T225942_20200821T005942.EOF']
