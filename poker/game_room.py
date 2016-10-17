from . import GameEventListener, GameError
import gevent
import logging
import threading


class FullGameRoomException(Exception):
    pass


class DuplicatePlayerException(Exception):
    pass


class GameRoomPlayers:
    def __init__(self, room_size):
        self._seats = [None] * room_size
        self._players = {}
        self._lock = threading.Lock()

    @property
    def players(self):
        self._lock.acquire()
        try:
            return [self._players[player_id] for player_id in self._seats if player_id is not None]
        finally:
            self._lock.release()

    @property
    def seats(self):
        return list(self._seats)

    def get_player(self, player_id):
        return self._players[player_id]

    def get_free_seat(self):
        try:
            return self._seats.index(None)
        except KeyError:
            raise FullGameRoomException

    def is_full(self):
        try:
            self.get_free_seat()
            return False
        except FullGameRoomException:
            return True

    def add_player(self, player):
        self._lock.acquire()
        try:
            if self._players.has_key(player.id):
                raise DuplicatePlayerException
            free_seat = self.get_free_seat()
            self._seats[free_seat] = player.id
            self._players[player.id] = player
        finally:
            self._lock.release()

    def remove_player(self, player_id):
        self._lock.acquire()
        try:
            seat = self._seats.index(player_id)
            self._seats[seat] = None
            del self._players[player_id]
        finally:
            self._lock.release()


class GameRoomEventHandler:
    def __init__(self, room_players, room_id, logger):
        self._room_players = room_players
        self._room_id = room_id
        self._logger = logger

    def room_event(self, event, player_id):
        self.broadcast({
            "message_type": "room-update",
            "event": event,
            "room_id": self._room_id,
            "players": {player.id: player.dto() for player in self._room_players.players},
            "player_ids": self._room_players.seats,
            "player_id": player_id
        })

    def broadcast(self, message):
        self._logger.info(message)
        for player in self._room_players.players:
            player.try_send_message(message)


class GameRoom(GameEventListener):
    def __init__(self, id, game_factory, room_size, logger):
        self._id = id
        self._game_factory = game_factory
        self._room_players = GameRoomPlayers(room_size)
        self._room_event_handler = GameRoomEventHandler(self._room_players, self._id, logger)
        self._event_messages = []
        self._active = False
        self._logger = logger
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._active

    def join(self, player):
        self._lock.acquire()
        try:
            try:
                self._room_players.add_player(player)
                self._room_event_handler.room_event("player-added", player.id)
            except DuplicatePlayerException:
                old_player = self._room_players.get_player(player.id)
                old_player.update_channel(player)
                player = old_player
                self._room_event_handler.room_event("player-rejoined", player.id)

            for event_message in self._event_messages:
                if "target" not in event_message or event_message["target"] == player.id:
                    player.send_message(event_message)
        finally:
            self._lock.release()

    def leave(self, player_id):
        self._lock.acquire()
        try:
            self._leave(player_id)
        finally:
            self._lock.release()

    def _leave(self, player_id):
        player = self._room_players.get_player(player_id)
        player.disconnect()
        self._room_players.remove_player(player.id)
        self._room_event_handler.room_event("player-removed", player.id)

    def game_event(self, event, event_data):
        self._lock.acquire()
        try:
            # Broadcast the event to the room
            event_message = {"message_type": "game-update", "event": event}
            event_message.update(event_data)

            if "target" in event_data:
                player = self._room_players.get_player(event_data["target"])
                player.send_message(event_message)
            else:
                # Broadcasting message
                self._room_event_handler.broadcast(event_message)

            if event == "game-over":
                self._event_messages = []
            else:
                self._event_messages.append(event_message)

            if event == "dead-player":
                self._leave(event_data["player"]["id"])
        finally:
            self._lock.release()

    def ping_all_players(self):
        for player in self._room_players.players:
            if not player.ping():
                self.leave(player.id)

    def activate(self):
        self._active = True
        try:
            self._logger.info("Activating room {}...".format(self._id))
            dealer_key = -1
            while True:
                try:
                    self.ping_all_players()

                    players = self._room_players.players
                    if len(players) < 2:
                        raise GameError("At least two players needed to start a new game")

                    dealer_key = (dealer_key + 1) % len(players)

                    game = self._game_factory.create_game(players)
                    game.event_dispatcher.subscribe(self)
                    game.play_hand(players[dealer_key].id)
                    game.event_dispatcher.unsubscribe(self)

                except GameError:
                    break
        finally:
            self._logger.info("Deactivating room {}...".format(self._id))
            self._active = False
