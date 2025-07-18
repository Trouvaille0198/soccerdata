"""Scraper for http://sofifa.com."""

import json
import re
from datetime import timedelta, datetime
from itertools import product
from pathlib import Path
from typing import Callable, Optional, Union

import pandas as pd
from lxml import html

from ._common import (
    BaseRequestsReader,
    add_standardized_team_name,
    safe_xpath_text,
    standardize_colnames,
)
from ._config import DATA_DIR, NOCACHE, NOSTORE, TEAMNAME_REPLACEMENTS, logger, MAXAGE

SO_FIFA_DATADIR = DATA_DIR / "SoFIFA"
SO_FIFA_API = "https://sofifa.com"


class SoFIFA(BaseRequestsReader):
    """Provides pd.DataFrames from data at http://sofifa.com.

    Data will be downloaded as necessary and cached locally in
    ``~/soccerdata/data/SoFIFA``.

    Parameters
    ----------
    leagues : string or iterable, optional
        IDs of leagues to include.
    versions : string, int or list of int, optional
        FIFA releases to include. Should be specified by their ID used in the URL
        (e.g., 230034). Alternatively, the string "all" can be used to include all
        versions and "latest" to include the latest version only. Defaults to
        "latest".
    proxy : 'tor' or or dict or list(dict) or callable, optional
        Use a proxy to hide your IP address. Valid options are:
            - "tor": Uses the Tor network. Tor should be running in
              the background on port 9050.
            - str: The address of the proxy server to use.
            - list(str): A list of proxies to choose from. A different proxy will
              be selected from this list after failed requests, allowing rotating
              proxies.
            - callable: A function that returns a valid proxy. This function will
              be called after failed requests, allowing rotating proxies.
    no_cache : bool
        If True, will not use cached data.
    no_store : bool
        If True, will not store downloaded data.
    data_dir : Path
        Path to directory where data will be cached.
    """

    def __init__(
        self,
        leagues: Optional[Union[str, list[str]]] = None,
        versions: Union[str, int, list[int]] = "latest",
        proxy: Optional[Union[str, list[str], Callable[[], str]]] = None,
        no_cache: bool = NOCACHE,
        no_store: bool = NOSTORE,
        data_dir: Path = SO_FIFA_DATADIR,
    ):
        """Initialize SoFIFA reader."""
        super().__init__(
            leagues=leagues,
            proxy=proxy,
            no_cache=no_cache,
            no_store=no_store,
            data_dir=data_dir,
        )
        self.rate_limit = 1
        if versions == "latest":
            self.versions = self.read_versions().tail(n=1)
        elif versions == "all":
            self.versions = self.read_versions()
        elif isinstance(versions, int):
            self.versions = self.read_versions().loc[[versions]]
        elif isinstance(versions, list) and all(isinstance(v, int) for v in versions):
            self.versions = self.read_versions().loc[versions]
        else:
            raise ValueError(f"Invalid value for versions: {versions}")

    def read_leagues(self) -> pd.DataFrame:
        """Retrieve selected leagues from the datasource.

        Returns
        -------
        pd.DataFrame
        """
        # read home page (overview)
        filepath = self.data_dir / "leagues.json"
        urlmask = SO_FIFA_API + "/api/league"
        reader = self.get(urlmask, filepath)
        response = json.load(reader)

        # extract league links
        leagues = []
        for node in response["data"]:
            for child in node["childs"]:
                leagues.append(
                    {
                        "league_id": child["id"],
                        "league": f"[{child['nationName']}] {child['value']}",
                    }
                )
        return (
            pd.DataFrame(leagues)
            .pipe(self._translate_league)
            .set_index("league")
            .sort_index()
            .loc[self._selected_leagues.keys()]
        )

    def read_versions(self, max_age: Union[int, timedelta] = MAXAGE) -> pd.DataFrame:
        """Retrieve available FIFA releases and rating updates.

        Parameters
        ----------
        max_age : int for age in days, or timedelta object
            The max. age of the locally cached release history before a new
            version is downloaded.

        Raises
        ------
        TypeError
            If max_age is not an integer or timedelta object.

        Returns
        -------
        pd.DataFrame
        """
        # read home page (overview)
        filepath = self.data_dir / "index.html"
        reader = self.get(SO_FIFA_API, filepath, max_age)

        # extract FIFA releases
        versions = []
        tree = html.parse(reader)
        for i, node_fifa_edition in enumerate(tree.xpath("//header/section/p/select[1]/option")):
            fifa_edition = node_fifa_edition.text
            filepath = self.data_dir / f"updates_{fifa_edition}.html"
            url = SO_FIFA_API + node_fifa_edition.get("value")
            # check for updates on latest FIFA edition only
            reader = self.get(url, filepath, max_age=max_age if i == 0 else None)
            tree = html.parse(reader)

            for node_fifa_update in tree.xpath("//header/section/p/select[2]/option"):
                href = node_fifa_update.get("value")
                version_id = re.search(r"r=(\d+)", href).group(1)  # type: ignore
                versions.append(
                    {
                        "version_id": int(version_id),
                        "fifa_edition": fifa_edition,
                        "update": node_fifa_update.text,
                    }
                )
        return pd.DataFrame(versions).set_index("version_id").sort_index()

    def read_teams(self) -> pd.DataFrame:
        """Retrieve all teams for the selected leagues.

        Returns
        -------
        pd.DataFrame
        """
        # build url
        urlmask = SO_FIFA_API + "/teams?lg={}&r={}&set=true"
        filemask = "teams_{}_{}.html"

        # get league IDs
        leagues = self.read_leagues()

        # collect teams
        teams = []
        iterator = list(product(leagues.iterrows(), self.versions.iterrows()))
        for i, ((lkey, league), (version_id, version)) in enumerate(iterator):
            logger.info(
                "[%s/%s] Retrieving teams for %s in %s edition",
                i + 1,
                len(iterator),
                lkey,
                version["update"],
            )
            league_id = league["league_id"]
            # read html page (league overview)
            filepath = self.data_dir / filemask.format(league_id, version_id)
            url = urlmask.format(league_id, version_id)
            reader = self.get(url, filepath)

            # extract team links
            tree = html.parse(reader)
            pat_team = re.compile(r"\/team\/(\d+)\/[\w-]+\/")
            for node in tree.xpath("//table/tbody/tr"):
                # extract team IDs from links
                team_link = node.xpath(".//td[2]//a")[0]
                teams.append(
                    {
                        "team_id": int(
                            re.search(pat_team, team_link.get("href")).group(1)  # type: ignore
                        ),
                        "team": team_link.text,
                        "league": lkey,
                        **version.to_dict(),
                    }
                )

        # return data frame
        return pd.DataFrame(teams).replace({"team": TEAMNAME_REPLACEMENTS}).set_index(["team_id"])

    def read_players(self, team: Optional[Union[str, list[str]]] = None) -> pd.DataFrame:
        """Retrieve all players for the selected leagues.

        Parameters
        ----------
        team: str or list of str, optional
            Team(s) to retrieve. If None, will retrieve all teams.

        Raises
        ------
        ValueError
            If no data is found for the given team(s) in the selected leagues.

        Returns
        -------
        pd.DataFrame
        """
        # build url
        urlmask = SO_FIFA_API + "/team/{}/?r={}&set=true"
        filemask = "players_{}_{}.html"

        # get list of teams
        df_teams = self.read_teams()

        if team is not None:
            teams_to_check = add_standardized_team_name(team)

            # select requested teams
            iterator = df_teams.loc[df_teams.team.isin(teams_to_check), :]
            if len(iterator) == 0:
                raise ValueError("No data found for the given teams in the selected seasons.")
        else:
            iterator = df_teams

        # collect players
        players = []
        iterator = list(product(self.versions.iterrows(), iterator.iterrows()))
        for i, ((version_id, version), (team_id, df_team)) in enumerate(iterator):
            logger.info(
                "[%s/%s] Retrieving list of players for %s in %s edition",
                i + 1,
                len(iterator),
                df_team["team"],
                version["update"],
            )

            # read html page (team overview)
            filepath = self.data_dir / filemask.format(team_id, version_id)
            url = urlmask.format(team_id, version_id)
            reader = self.get(url, filepath)

            # extract player links
            tree = html.parse(reader)
            pat_player = re.compile(r"\/player\/(\d+)\/[\w-]+\/")
            table_squad = tree.xpath("//article/table")
            for node in table_squad[0].xpath(".//td[2]/a[contains(@href,'/player/')]"):
                # extract player IDs from links
                # extract player names from links
                players.append(
                    {
                        "player_id": int(
                            re.search(pat_player, node.get("href")).group(1)  # type: ignore
                        ),
                        "player": node.get("data-tippy-content"),
                        "team": df_team["team"],
                        "league": df_team["league"],
                        **version.to_dict(),
                    }
                )

        # return data frame
        return pd.DataFrame(players).set_index(["player_id"])

    def read_team_ratings(self) -> pd.DataFrame:
        """Retrieve ratings for all teams in the selected leagues.

        Returns
        -------
        pd.DataFrame
        """
        # define id and description of ratings to retrieve
        ratings = {
            "oa": "overall",
            "at": "attack",
            "md": "midfield",
            "df": "defence",
            "tb": "transfer_budget",
            "cw": "club_worth",
            "bs": "build_up_speed",
            "bd": "build_up_dribbling",
            "bp": "build_up_passing",
            "bps": "build_up_positioning",
            "cc": "chance_creation_crossing",
            "cp": "chance_creation_passing",
            "cs": "chance_creation_shooting",
            "cps": "chance_creation_positioning",
            "da": "defence_aggression",
            "dm": "defence_pressure",
            "dw": "defence_team_width",
            "dd": "defence_defender_line",
            "dp": "defence_domestic_prestige",
            "ip": "international_prestige",
            "ps": "players",
            "sa": "starting_xi_average_age",
            "ta": "whole_team_average_age",
        }

        # build url
        urlmask = SO_FIFA_API + "/teams?lg={}&r={}&set=true"
        for rating_id in ratings:
            urlmask += f"&showCol[]={rating_id}"
        filemask = "teams_{}_{}.html"

        # get league IDs
        leagues = self.read_leagues()

        # collect teams
        teams = []
        iterator = list(product(leagues.iterrows(), self.versions.iterrows()))
        for i, ((lkey, league), (version_id, version)) in enumerate(iterator):
            logger.info(
                "[%s/%s] Retrieving teams for %s in %s edition",
                i + 1,
                len(iterator),
                lkey,
                version["update"],
            )
            league_id = league["league_id"]
            # read html page (league overview)
            filepath = self.data_dir / filemask.format(league_id, version_id)
            url = urlmask.format(league_id, version_id)
            reader = self.get(url, filepath)

            # extract team links
            tree = html.parse(reader)
            for node in tree.xpath("//table/tbody/tr"):
                # extract team IDs from links
                teams.append(
                    {
                        "league": lkey,
                        "team": node.xpath(".//td[2]//a")[0].text,
                        **{
                            desc: safe_xpath_text(
                                node,
                                f".//td[@data-col='{key}']//text()",
                                warn=f"Could not parse {desc} ({key}) stat.",
                            )
                            for key, desc in ratings.items()
                        },
                        **version.to_dict(),
                    }
                )

        # return data frame
        return (
            pd.DataFrame(teams)
            .replace({"team": TEAMNAME_REPLACEMENTS})
            .set_index(["league", "team"])
            .sort_index()
        )

    def read_player_ratings(
        self,
        team: Optional[Union[str, list[str]]] = None,
        player: Optional[Union[int, list[int]]] = None,
    ) -> pd.DataFrame:
        """Retrieve ratings for players.

        Parameters
        ----------
        team: str or list of str, optional
            Team(s) to retrieve. If None, will retrieve all teams.
        player: int or list of int, optional
            Player(s) to retrieve. If None, will retrieve all players.

        Returns
        -------
        pd.DataFrame
        """
        # build url
        urlmask = SO_FIFA_API + "/player/{}/?r={}&set=true"
        filemask = "player_{}_{}.html"

        # get player IDs
        if player is None:
            players = self.read_players(team=team).index.unique()
        elif isinstance(player, int):
            players = [player]
        else:
            players = player

        # prepare empty data frame
        ratings = []

        # define labels to use for score extraction from player profile pages
        score_labels = [
            "Overall rating",
            "Potential",
            "Value",
            "Wage",
            "Crossing",
            "Finishing",
            "Heading accuracy",
            "Short passing",
            "Volleys",
            "Dribbling",
            "Curve",
            "FK Accuracy",
            "Long passing",
            "Ball control",
            "Acceleration",
            "Sprint speed",
            "Agility",
            "Reactions",
            "Balance",
            "Shot power",
            "Jumping",
            "Stamina",
            "Strength",
            "Long shots",
            "Aggression",
            "Interceptions",
            "Positioning",
            "Vision",
            "Penalties",
            "Composure",
            "Defensive awareness",
            "Standing tackle",
            "Sliding tackle",
            "GK Diving",
            "GK Handling",
            "GK Kicking",
            "GK Positioning",
            "GK Reflexes",
        ]

        iterator = list(product(self.versions.iterrows(), players))
        for i, ((version_id, version), player) in enumerate(iterator):
            logger.info(
                "[%s/%s] Retrieving ratings for player with ID %s in %s edition",
                i + 1,
                len(iterator),
                player,
                version["update"],
            )

            # read html page (player overview)
            filepath = self.data_dir / filemask.format(player, version_id)
            url = urlmask.format(player, version_id)
            reader = self.get(url, filepath)

            # extract scores one-by-one
            tree = html.parse(reader, parser=html.HTMLParser(encoding="utf8"))
            node_player_name = tree.xpath("//div[contains(@class, 'profile')]/h1")[0]
            # Extract what is before <br>
            before_br = node_player_name.xpath("string(./text()[1])").strip()
            # Extract what is after <br>
            after_br = node_player_name.xpath("string(./br/following-sibling::text()[1])").strip()
            scores = {
                "player": before_br if before_br else after_br,
                **version.to_dict(),
            }

            # region basic info
            simple_name=tree.xpath("/html/body/header/section/h1/text()")[0]
            scores["simple_name"]=simple_name

            basic_info_p=tree.xpath("//div[contains(@class, 'profile')]/p/text()")
            basic_info=next((b for b in basic_info_p if b.strip()), None)
            if not basic_info:
                raise ValueError(f"Basic information not found for player {player}")
            basic_info_match = re.match(
    r"^(\d+)y\.o\..*?\((\w+\s\d{1,2},\s\d{4})[^)]*\)\s(\d+)cm.*\s(\d+)kg.*?$",
    basic_info,
)
            if not basic_info_match:
                raise ValueError(f"Basic information '{basic_info}' not recognized for player {player}")

            scores["age"] = int(basic_info_match.group(1))
            scores["birthdate"] = datetime.strptime(basic_info_match.group(2), "%b %d, %Y").strftime("%m-%d")
            scores["height"] = int(basic_info_match.group(3))
            scores["weight"] = int(basic_info_match.group(4))

            scores["pos"]=tree.xpath("//div[contains(@class, 'profile')]/p/span/text()")[0]
            scores["avatar"] = tree.xpath("//div[contains(@class, 'profile')]/img/@data-src")[0]
            name_line=tree.xpath("//*[@id='head']/title/text()")[0]
            scores["name"]=name_line.split("-")[0].strip()

            capa=tree.xpath(r"//*[@id='body']/script[2]/text()")[0]
            scores["pac"] =re.search(r"POINT_PAC=(\d+)", capa).group(1)
            scores["sho"] = re.search(r"POINT_SHO=(\d+)", capa).group(1)
            scores["pas"] = re.search(r"POINT_PAS=(\d+)", capa).group(1)
            scores["dri"] = re.search(r"POINT_DRI=(\d+)", capa).group(1)
            scores["def"] = re.search(r"POINT_DEF=(\d+)", capa).group(1)
            scores["phy"] = re.search(r"POINT_PHY=(\d+)", capa).group(1)

            # Try each XPath until one returns a result
            for s in score_labels:
                value = None
                xpaths = [
                    f"//p[.//text()[contains(.,'{s}')]]/span/em",
                    f"//div[contains(.,'{s}')]/em",
                    f"//li[not(self::script)][.//text()[contains(.,'{s}')]]/em",
                ]
                for xpath in xpaths:
                    nodes = tree.xpath(xpath)
                    if nodes:  # If at least one match is found
                        value = nodes[0].text.strip()  # Take only the first match
                        break  # Stop checking other XPaths once we find a valid value

                scores[s] = value if value is not None else None  # Assign only once
            scores["Wage"] = remove_currency(scores["Wage"])
            scores["Value"] = remove_currency(scores["Value"])

            ratings.append(scores)
            # endregion
        # return data frame
        return pd.DataFrame(ratings).pipe(standardize_colnames).set_index(["player"]).sort_index()

def remove_currency(s):
    digital_part = re.sub(r'[^\d\.]', '', s)
    val = float(digital_part)
    if 'M' in s:
        return str(int(val * 1000000))
    elif 'K' in s:
        return str(int(val * 1000))
    else:
        return str(int(val))