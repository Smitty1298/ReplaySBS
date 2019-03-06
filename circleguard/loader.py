from datetime import datetime
import time
import base64
import sys

from requests import RequestException
import osrparse
import osuAPI

from online_replay import OnlineReplay
from user_info import UserInfo
from enums import Error
from exceptions import (InvalidArgumentsException, APIException, CircleguardException,
                        RatelimitException, InvalidKeyException, ReplayUnavailableException, UnknownAPIException)

def request(function):
    """
    Decorator intended to appropriately handle all request and api related exceptions.
    """

    def wrapper(*args, **kwargs):
        # catch them exceptions boy
        ret = None
        try:
            ret = function(*args, **kwargs)
        except RatelimitException:
            args[0].enforce_ratelimit()
            # wrap function with the decorator then call decorator
            ret = request(function)(*args, **kwargs)
        except InvalidKeyException as e:
            print(str(e))
            sys.exit(0)
        except RequestException as e:
            print("Request exception: {}. Sleeping for 5 seconds then retrying".format(e))
            time.sleep(5)
            ret = request(function)(*args, **kwargs)
        except ReplayUnavailableException as e:
            print(str(e))
            ret = None
        return ret
    return wrapper

def api(function):
    """
    Decorator that checks if we can refresh the time at which we started our requests because
    it's been more than RATELIMIT_RESET since the first request of the cycle.

    If we've refreshed our ratelimits, sets start_time to be the current datetime.
    """
    def wrapper(*args, **kwargs):
        # check if we've refreshed our ratelimits yet
        difference = datetime.now() - Loader.start_time
        if(difference.seconds > Loader.RATELIMIT_RESET):
            Loader.start_time = datetime.now()
        return function(*args, **kwargs)
    return wrapper


def check_cache(function):
    """
    Decorator that checks if the replay by the given user_id on the given map_id is already cached.
    If so, returns a Replay instance from the cached string instead of requesting it from the api.

    Note that self, cacher and user_info must be the first, second and third arguments to the function respectively.

    Returns:
        A Replay instance from the cached replay if it was cached, or the return value of the function if not.
    """

    def wrapper(*args, **kwargs):
        self = args[0]
        cacher = args[1]
        user_info = args[2]

        lzma = cacher.check_cache(user_info.map_id, user_info.user_id, user_info.enabled_mods)
        if(lzma):
            replay_data = osrparse.parse_replay(lzma, pure_lzma=True).play_data
            self.loaded += 1
            return OnlineReplay(replay_data, user_info)
        else:
            return function(*args, **kwargs)
    return wrapper

class Loader():
    """
    Manages interactions with the osu api - if the api ratelimits the key we wait until we refresh our ratelimits
    and retry the request.

    This class is not meant to be instantiated, instead only static methods and class variables used.
    This is because we only use one api key for the entire project, and making all methods static provides
    cleaner access than passing around a single Loader class.
    """

    RATELIMIT_RESET = 60 # time in seconds until the api refreshes our ratelimits
    start_time = datetime.min # when we started our requests cycle


    def __init__(self, key):
        """
        Initializes a Loader instance.
        """

        self.total = None
        self.loaded = 0
        self.api = osuAPI.OsuAPI(key)


    def new_session(self, total):
        """
        Resets the loaded replays to 0, and sets the total to the passed total.

        Intended to be called every time the loader is used for a different set of replay loadings -
        since a Loader instance is passed around to Comparer and Investigator, each with different amounts
        of replays to load, making new sessions is necessary to keep progress logs correct.
        """

        self.loaded = 0
        self.total = total


    @request
    @api
    def user_info(self, map_id, num=None, user_id=None, mods=None, limit=True):
        """
        Returns a list of UserInfo objects containing a user's (user_id, username, replay_id, enabled mods, replay available) on a given map.

        Args:
            Integer map_id: The map id to get the replay_id from.
            Integer user_id: The user id to get the replay_id from.
            Boolean limit: If set, will only return a user's top score (top response). Otherwise, will return every response (every score they set on that map under different mods)
            Integer mods: The mods the replay info to retieve were played with.
        """

        if(num and (num > 100 or num < 2)):
            raise InvalidArgumentsException("The number of top plays to fetch must be between 2 and 100 inclusive!")

        if(not bool(user_id) ^ bool(num)):
            raise InvalidArgumentsException("One of either num or user_id must be passed, but not both")

        response = self.api.get_scores({"m": "0", "b": map_id, "limit": num, "u": user_id, "mods": mods})
        Loader.check_response(response)
                                                                    # yes, it's necessary to cast the str response to int before bool - all strings are truthy.
        infos = [UserInfo(map_id, int(x["user_id"]), str(x["username"]), int(x["score_id"]), int(x["enabled_mods"]), bool(int(x["replay_available"]))) for x in response]

        return infos[0:1] if (limit and user_id) else infos # limit only applies if user_id was set


    # def user_info_from_modset(self, map_id, user_id, mods=None, limit=True)
    @request
    @api
    def replay_data(self, user_info):
        """
        Queries the api for replay data from the given user on the given map, with the given mods.

        Args:
            UserInfo user_info: The UserInfo representing this replay.
        Returns:
            The lzma bytes (b64 decoded response) returned by the api, or None if the replay was not available.

        Raises:
            CircleguardException if the loader instance has had a new session made yet.
            APIException if the api responds with an error we don't know.
        """

        if(self.total is None):
            raise CircleguardException("loader#new_session(total) must be called after instantiation, before any replay data is loaded.")

        print("requesting replay by {} on map {} with mods {}".format(user_info.user_id, user_info.map_id, user_info.enabled_mods))
        response = self.api.get_replay({"m": "0", "b": user_info.map_id, "u": user_info.user_id, "mods": user_info.enabled_mods})

        Loader.check_response(response)
        self.loaded += 1

        return base64.b64decode(response["content"])

    @request
    @api
    def get_user_best(self, user_id, number):
        """
        Gets the top 100 best plays for the given user.

        Args:
            String user_id: The user id to get best plays of.
            Integer number: The number of top plays to retrieve. Must be between 1 and 100.

        Returns:
            A list of map_ids for the given number of the user's top plays.

        Raises:
            InvalidArgumentsException if number is not between 1 and 100 inclusive.
        """

        print("requesting top scores of {}".format(user_id))
        if(number < 1 or number > 100):
            raise InvalidArgumentsException("The number of best user plays to fetch must be between 1 and 100 inclusive!")
        response = self.api.get_user_best({"m": "0", "u": user_id, "limit": number})

        Loader.check_response(response)

        return response

    @api
    def replay_from_user_info(self, cacher, user_info):
        """
        Creates a list of Replay instances for the users listed in user_info on the given map.

        Args:
            Cacher cacher: A cacher object containing a database connection.
            List [UserInfo]: A list of UserInfo objects, representing where and how to retrieve the replays.

        Returns:
            A list of Replay instances from the given information. Some entries may be none if there was no replay data
            available - see loader#replay_from_map.
        """

        replays = [self.replay_from_map(cacher, info) for info in user_info]
        return replays

    @api
    @check_cache
    def replay_from_map(self, cacher, user_info):
        """
        Creates an OnlineReplay instance from a replay by the given user on the given map.

        Args:
            Cacher cacher: A cacher object containing a database connection.
            UserInfo user_info: The UserInfo object representing this replay.

        Returns:
            The Replay instance created with the given information, or None if the replay was not available.

        Raises:
            UnknownAPIException if replay_available was 1, but we did not receive replay data from the api.
        """

        if(not user_info.replay_available):
            return None

        lzma_bytes = self.replay_data(user_info)
        if(lzma_bytes is None):
            raise UnknownAPIException("The api guaranteed there would be a replay available, but we did not receive any data. "
                                     "Please report this to the devs, who will open an issue on osu!api if necessary.")
        parsed_replay = osrparse.parse_replay(lzma_bytes, pure_lzma=True)
        replay_data = parsed_replay.play_data
        cacher.cache(lzma_bytes, user_info)
        return OnlineReplay(replay_data, user_info)

    @staticmethod
    def check_response(response):
        """
        Checks the given api response for any kind of error or unexpected response.

        Args:
            String response: The api-returned response to check.

        Raises:
            An Error corresponding to the type of error if there was an error.
        """

        if("error" in response):
            for error in Error:
                if(response["error"] == error.value[0]):
                    raise error.value[1](error.value[2])
            else:
                raise Error.UNKNOWN.value[1](Error.UNKNOWN.value[2])

    def enforce_ratelimit(self):
        """
        Enforces the ratelimit by sleeping the thread until it's safe to make requests again.
        """

        difference = datetime.now() - Loader.start_time
        seconds_passed = difference.seconds
        if(seconds_passed > Loader.RATELIMIT_RESET):
            return

        # sleep the remainder of the reset cycle so we guarantee it's been that long since the first request
        sleep_seconds = Loader.RATELIMIT_RESET - seconds_passed
        print(f"ratelimited, sleeping for {sleep_seconds} seconds. "
              f"{self.loaded} of {self.total} replays loaded. ETA ~ {int((self.total-self.loaded)/10)+1} min")
        time.sleep(sleep_seconds)