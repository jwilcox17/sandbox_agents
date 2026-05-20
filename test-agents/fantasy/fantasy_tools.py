import os
import re
import logging
import json
import requests
import asyncio
from typing import Dict, List, Union, Any, Optional

from fantasy_config import FantasyConfigManager, DEFAULT_CONFIG_PATH

class FantasyBaseballTools:
    """Tools for Fantasy Baseball MCP server with MLB data integration"""

    def __init__(self, logger: logging.Logger = None):
        """
        Initialize FantasyBaseballTools with optional logging

        Args:
            logger (logging.Logger, optional): Logger for tracking operations
        """
        self.logger = logger or logging.getLogger(__name__)
        self.mlb_stats_api_url = "https://statsapi.mlb.com/api"
        self.config_manager = FantasyConfigManager(logger=self.logger)

    def get_fantasy_config(self, config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
        """
        Get the fantasy baseball league configuration

        Args:
            config_path (str, optional): Path to the configuration file

        Returns:
            Dict containing the configuration or error information
        """
        return self.config_manager.read_config(config_path)

    def get_team_roster(self, team_name: str, config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
        """
        Get a team's roster from the fantasy league configuration

        Args:
            team_name (str): Name of the team to retrieve
            config_path (str, optional): Path to the configuration file

        Returns:
            Dict containing the team roster or error information
        """
        result = self.config_manager.get_team(team_name, config_path)
        if "error" in result:
            return result

        team = result["team"]
        return {
            "team_name": team.get("name", "Unknown"),
            "hitters": team.get("hitters", []),
            "pitchers": team.get("pitchers", [])
        }

    def search_players(self, name: str) -> Dict[str, Any]:
        """
        Search for MLB players by name

        Args:
            name (str): Player name to search for

        Returns:
            Dict containing search results or error information
        """
        try:
            sanitized_name = re.sub(r'[^\w\s]', '', name)

            url = f"{self.mlb_stats_api_url}/v1/people/search"
            params = {"names": sanitized_name, "limit": 10}

            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            players = data.get("people", [])
            self.logger.info(f"Found {len(players)} players matching '{name}'")

            return {"results": players}

        except requests.RequestException as e:
            error_msg = f"API request error during player search: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

        except Exception as e:
            error_msg = f"Unexpected error in player search: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def search_multiple_players(self, names: List[str]) -> Dict[str, Any]:
        """
        Search for multiple MLB players by name in a single call

        Args:
            names (List[str]): List of player names to search for

        Returns:
            Dict containing search results or error information
        """
        try:
            if not names:
                return {"error": "No player names provided"}

            all_results = {}

            for name in names:
                sanitized_name = re.sub(r'[^\w\s]', '', name)

                url = f"{self.mlb_stats_api_url}/v1/people/search"
                params = {"names": sanitized_name, "limit": 5}  # Limiting to 5 per player for readability

                response = requests.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                players = data.get("people", [])
                self.logger.info(f"Found {len(players)} players matching '{name}'")

                all_results[name] = players

            self.logger.info(f"Successfully searched for {len(names)} player names")
            return {"results": all_results}

        except requests.RequestException as e:
            error_msg = f"API request error during multiple player search: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

        except Exception as e:
            error_msg = f"Unexpected error in multiple player search: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def get_player_data(self, player_id: int, season: Optional[int] = 2025) -> Dict[str, Any]:
        """
        Get comprehensive player profile and statistics

        Args:
            player_id (int): MLB player ID
            season (int, optional): Season year (default: 2025)

        Returns:
            Dict containing player data or error information
        """
        try:
            player_url = f"{self.mlb_stats_api_url}/v1/people/{player_id}"
            params = {"hydrate": "currentTeam,stats"}

            if season:
                params["season"] = season

            response = requests.get(player_url, params=params)
            response.raise_for_status()
            data = response.json()

            if "people" not in data or not data["people"]:
                return {"error": f"No player found with ID {player_id}"}

            player_info = data["people"][0]

            # Get additional stats if not already included
            if "stats" not in player_info and season:
                stats_url = f"{self.mlb_stats_api_url}/v1/people/{player_id}/stats"
                stats_params = {
                    "stats": "season",
                    "season": season,
                    "group": "hitting,pitching,fielding"
                }

                stats_response = requests.get(stats_url, params=stats_params)
                stats_data = stats_response.json()
                player_info["stats"] = stats_data.get("stats", [])

            self.logger.info(f"Successfully retrieved data for player {player_id}")
            return {"player": player_info}

        except requests.RequestException as e:
            error_msg = f"API request error retrieving player data: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

        except Exception as e:
            error_msg = f"Unexpected error retrieving player data: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def get_multiple_players_data(self, player_ids: List[int], season: Optional[int] = 2025) -> Dict[str, Any]:
        """
        Get comprehensive profiles and statistics for multiple players in a single call

        Args:
            player_ids (List[int]): List of MLB player IDs
            season (int, optional): Season year (default: 2025)

        Returns:
            Dict containing player data or error information
        """
        try:
            if not player_ids:
                return {"error": "No player IDs provided"}

            players_data = []
            for player_id in player_ids:
                result = self.get_player_data(player_id, season)
                if "player" in result:
                    players_data.append(result["player"])
                elif "error" in result:
                    self.logger.warning(f"Error fetching player {player_id}: {result['error']}")

            if not players_data:
                return {"error": "Could not retrieve data for any of the requested players"}

            self.logger.info(f"Successfully retrieved data for {len(players_data)} players")
            return {"players": players_data}

        except Exception as e:
            error_msg = f"Unexpected error retrieving multiple player data: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def get_league_leaders(self, stat_type: str, season: Optional[int] = 2025) -> Dict[str, Any]:
        """
        Get league leaders for a specific statistic

        Args:
            stat_type (str): Statistic to get leaders for (e.g., homeRuns, battingAverage)
            season (int, optional): Season year (default: 2025)

        Returns:
            Dict containing leader data or error information
        """
        try:
            stat_mapping = {
                "hr": "homeRuns",
                "avg": "battingAverage",
                "rbi": "rbi",
                "sb": "stolenBases",
                "wins": "wins",
                "era": "earnedRunAverage",
                "so": "strikeOuts",
                "whip": "whip"
                # Add more mappings as needed
            }

            # Use mapping if available, otherwise use as-is
            actual_stat = stat_mapping.get(stat_type.lower(), stat_type)

            params = {
                "leaderCategories": actual_stat,
                "limit": 10
            }

            if season:
                params["season"] = season

            url = f"{self.mlb_stats_api_url}/v1/stats/leaders"
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            leader_data = []
            if "leagueLeaders" in data:
                for category in data["leagueLeaders"]:
                    if category.get("leaderCategory") == actual_stat:
                        leader_data = category.get("leaders", [])
                        break

            self.logger.info(f"Retrieved {len(leader_data)} leaders for {actual_stat}")
            return {"leaders": leader_data, "stat_type": actual_stat}

        except requests.RequestException as e:
            error_msg = f"API request error retrieving league leaders: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

        except Exception as e:
            error_msg = f"Unexpected error retrieving league leaders: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def recommend_daily_lineup(self, player_ids: List[int]) -> Dict[str, Any]:
        """
        Get recommendations for which players to start in daily lineup

        Args:
            player_ids (List[int]): List of player IDs in your roster

        Returns:
            Dict containing recommendations or error information
        """
        try:
            if not player_ids:
                return {"error": "No player IDs provided"}

            # Get data for all players
            player_data = []
            for player_id in player_ids:
                result = self.get_player_data(player_id)
                if "player" in result:
                    player_data.append(result["player"])

            # This is where we would implement complex lineup optimization logic
            # For now, using a simple placeholder recommendation system
            start_recommendations = []
            bench_recommendations = []

            for player in player_data:
                # Placeholder logic
                player_info = {
                    "id": player.get("id"),
                    "name": player.get("fullName"),
                    "position": player.get("primaryPosition", {}).get("abbreviation", "Unknown"),
                    "team": player.get("currentTeam", {}).get("name", "Unknown")
                }

                # Simple alternating recommendation for demo purposes
                if len(start_recommendations) < len(player_data) * 0.7:
                    start_recommendations.append(player_info)
                else:
                    bench_recommendations.append(player_info)

            self.logger.info(f"Generated lineup recommendations for {len(player_data)} players")
            return {
                "start": start_recommendations,
                "bench": bench_recommendations
            }

        except Exception as e:
            error_msg = f"Error generating lineup recommendations: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def identify_waiver_pickups(self, available_player_ids: List[int]) -> Dict[str, Any]:
        """
        Get recommendations for waiver wire pickups

        Args:
            available_player_ids (List[int]): List of player IDs available on the waiver wire

        Returns:
            Dict containing recommendations or error information
        """
        try:
            if not available_player_ids:
                return {"error": "No player IDs provided"}

            # Get data for available players
            available_players = []
            for player_id in available_player_ids:
                result = self.get_player_data(player_id)
                if "player" in result:
                    available_players.append(result["player"])

            # Placeholder recommendation logic
            recommendations = []

            for player in available_players[:5]:  # Limit to top 5 for example
                player_info = {
                    "id": player.get("id"),
                    "name": player.get("fullName"),
                    "position": player.get("primaryPosition", {}).get("abbreviation", "Unknown"),
                    "team": player.get("currentTeam", {}).get("name", "Unknown"),
                    "reason": "Recent strong performance" # Placeholder
                }
                recommendations.append(player_info)

            self.logger.info(f"Generated waiver pickup recommendations from {len(available_players)} players")
            return {"recommendations": recommendations}

        except Exception as e:
            error_msg = f"Error generating waiver pickup recommendations: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}

    def evaluate_trade(self, players_to_give: List[int], players_to_receive: List[int]) -> Dict[str, Any]:
        """
        Evaluate a potential trade between fantasy teams

        Args:
            players_to_give (List[int]): List of player IDs you are giving up in the trade
            players_to_receive (List[int]): List of player IDs you are receiving in the trade

        Returns:
            Dict containing trade evaluation or error information
        """
        try:
            if not players_to_give or not players_to_receive:
                return {"error": "Invalid trade parameters"}

            # Get data for all players involved
            giving_players = []
            for player_id in players_to_give:
                result = self.get_player_data(player_id)
                if "player" in result:
                    giving_players.append(result["player"])

            receiving_players = []
            for player_id in players_to_receive:
                result = self.get_player_data(player_id)
                if "player" in result:
                    receiving_players.append(result["player"])

            # Placeholder trade evaluation logic - in a real implementation,
            # we would compare stats, positions, team needs, etc.
            giving_value = len(giving_players) * 10  # Placeholder value
            receiving_value = len(receiving_players) * 10  # Placeholder value

            assessment = ""
            if receiving_value > giving_value * 1.1:
                assessment = "This trade is favorable for you."
            elif giving_value > receiving_value * 1.1:
                assessment = "This trade is unfavorable for you."
            else:
                assessment = "This trade is relatively even."

            self.logger.info(f"Evaluated trade with {len(giving_players)} players given and {len(receiving_players)} received")
            return {
                "giving": [{"id": p.get("id"), "name": p.get("fullName")} for p in giving_players],
                "receiving": [{"id": p.get("id"), "name": p.get("fullName")} for p in receiving_players],
                "assessment": assessment
            }

        except Exception as e:
            error_msg = f"Error evaluating trade: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}
        
    
