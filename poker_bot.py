"""
Texas Hold'em Discord bot (server-based).

Run /poker in a channel to open a table. Players hit Join, host hits Start.
Hole cards are private (My Cards button); board, pot and turn are shared.
Fold / Check / Call / Raise via buttons. Showdown auto-evaluates and pays out.

Setup:
  pip install -U "discord.py" treys python-dotenv
  Put your token in a .env file:  DISCORD_BOT_TOKEN=your_token_here
  Enable the "Message Content Intent" is NOT required (slash + buttons only).
  Invite with scopes: bot + applications.commands, perms: Send Messages, Embed Links.
  Run:  python poker_bot.py
"""

import os
import asyncio
from treys import Card, Deck, Evaluator

STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20

# ----------------------------------------------------------------------------
# GAME ENGINE  (pure python, no discord imports so it can be tested standalone)
# ----------------------------------------------------------------------------

SUITS = {"s": "\u2660", "h": "\u2665", "d": "\u2666", "c": "\u2663"}


def card_str(card_int) -> str:
    """Render a treys card int as e.g. 'K\u2665'."""
    s = Card.int_to_str(card_int)  # e.g. 'Kh'
    rank, suit = s[0], s[1]
    rank = "10" if rank == "T" else rank
    return f"{rank}{SUITS[suit]}"


class Player:
    def __init__(self, user_id, name, chips=STARTING_CHIPS):
        self.user_id = user_id
        self.name = name
        self.chips = chips
        self.hole = []            # two treys card ints
        self.in_hand = True       # False once folded
        self.bet = 0              # committed this street
        self.committed = 0        # committed this whole hand (for side pots)
        self.all_in = False
        self.acted = False        # has acted since the last raise this street

    def reset_for_hand(self):
        self.hole = []
        self.in_hand = self.chips > 0
        self.bet = 0
        self.committed = 0
        self.all_in = False
        self.acted = False

    def reset_for_street(self):
        self.bet = 0
        self.acted = False


class Game:
    STREETS = ["preflop", "flop", "turn", "river"]

    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.players = []          # seating order
        self.button = -1           # dealer button index
        self.deck = None
        self.board = []            # community cards
        self.pot = 0               # chips already collected from finished streets
        self.current_bet = 0       # highest bet this street
        self.min_raise = BIG_BLIND # smallest legal raise increment
        self.street = None
        self.to_act = None         # index of player to act
        self.in_progress = False
        self.last_winner_text = ""

    # ---- lobby ----
    def add_player(self, user_id, name):
        if any(p.user_id == user_id for p in self.players):
            return False
        if self.in_progress:
            return False
        self.players.append(Player(user_id, name))
        return True

    def remove_player(self, user_id):
        if self.in_progress:
            return False
        before = len(self.players)
        self.players = [p for p in self.players if p.user_id != user_id]
        return len(self.players) != before

    def get(self, user_id):
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    # ---- hand lifecycle ----
    def seated(self):
        """Players with chips who can play a hand."""
        return [p for p in self.players if p.chips > 0]

    def start_hand(self):
        live = self.seated()
        if len(live) < 2:
            raise ValueError("Need at least 2 players with chips.")
        self.in_progress = True
        self.board = []
        self.pot = 0
        self.deck = Deck()
        for p in self.players:
            p.reset_for_hand()
        # rotate button among players who still have chips
        self.button = (self.button + 1) % len(self.players)
        while self.players[self.button].chips <= 0:
            self.button = (self.button + 1) % len(self.players)

        order = self._active_order(start=self.button + 1)
        # deal two cards each
        for p in order:
            p.hole = self.deck.draw(2)

        # blinds: heads-up posts differently but we keep standard for >=2
        sb = order[0]
        bb = order[1] if len(order) > 1 else order[0]
        self._post(sb, SMALL_BLIND)
        self._post(bb, BIG_BLIND)
        self.current_bet = BIG_BLIND
        self.min_raise = BIG_BLIND
        self.street = "preflop"
        # first to act preflop is left of big blind
        bb_index = self.players.index(bb)
        self.to_act = self._next_index(bb_index)

    def _active_order(self, start):
        """Players still in the hand, clockwise starting at index `start`."""
        n = len(self.players)
        order = []
        for i in range(n):
            idx = (start + i) % n
            p = self.players[idx]
            if p.in_hand:
                order.append(p)
        return order

    def _post(self, player, amount):
        amount = min(amount, player.chips)
        player.chips -= amount
        player.bet += amount
        player.committed += amount
        if player.chips == 0:
            player.all_in = True

    # ---- turn pointer ----
    def _eligible(self, p):
        return p.in_hand and not p.all_in

    def _next_index(self, from_index):
        n = len(self.players)
        for i in range(1, n + 1):
            idx = (from_index + i) % n
            if self._eligible(self.players[idx]):
                return idx
        return None

    def current(self):
        if self.to_act is None:
            return None
        return self.players[self.to_act]

    def legal_actions(self):
        p = self.current()
        if not p:
            return {}
        to_call = self.current_bet - p.bet
        actions = {"fold": True}
        if to_call <= 0:
            actions["check"] = True
        else:
            actions["call"] = min(to_call, p.chips)
        # can raise only if you have chips beyond the call
        if p.chips > to_call:
            actions["raise"] = True
        return actions

    def act(self, user_id, action, raise_to=None):
        p = self.current()
        if not p or p.user_id != user_id:
            raise ValueError("It's not your turn.")
        to_call = self.current_bet - p.bet

        if action == "fold":
            p.in_hand = False
            p.acted = True

        elif action == "check":
            if to_call > 0:
                raise ValueError("You can't check, there's a bet to you.")
            p.acted = True

        elif action == "call":
            self._post(p, to_call)
            p.acted = True

        elif action == "raise":
            # raise_to is the total this player wants their street bet to become
            if raise_to is None:
                raise ValueError("Raise amount required.")
            max_to = p.bet + p.chips
            if raise_to >= max_to:               # treat as all-in
                raise_to = max_to
            else:
                min_total = self.current_bet + self.min_raise
                if raise_to < min_total:
                    raise ValueError(f"Minimum raise is to {min_total}.")
            increment = raise_to - self.current_bet
            self._post(p, raise_to - p.bet)
            if increment >= self.min_raise:
                self.min_raise = increment
            self.current_bet = max(self.current_bet, p.bet)
            p.acted = True
            # a raise reopens action for everyone else
            for other in self.players:
                if other is not p and self._eligible(other):
                    other.acted = False
        else:
            raise ValueError("Unknown action.")

        self._advance()

    def _advance(self):
        # hand ends immediately if only one player remains
        remaining = [p for p in self.players if p.in_hand]
        if len(remaining) == 1:
            self.to_act = None
            self.street = "complete"
            return
        if self._street_done():
            self._next_street()
        else:
            nxt = self._next_index(self.to_act)
            self.to_act = nxt
            if nxt is None:  # everyone left is all-in
                self._next_street()

    def _street_done(self):
        actives = [p for p in self.players if self._eligible(p)]
        if not actives:
            return True
        return all(p.acted and p.bet == self.current_bet for p in actives)

    def _collect(self):
        for p in self.players:
            self.pot += p.bet
            p.reset_for_street()
        self.current_bet = 0
        self.min_raise = BIG_BLIND

    def _next_street(self):
        self._collect()
        if self.street == "preflop":
            self.board += self.deck.draw(3)
            self.street = "flop"
        elif self.street == "flop":
            self.board += self.deck.draw(1)
            self.street = "turn"
        elif self.street == "turn":
            self.board += self.deck.draw(1)
            self.street = "river"
        else:
            self.street = "complete"
            self.to_act = None
            return
        # first to act postflop: first eligible left of button
        first = self._next_index(self.button)
        self.to_act = first
        if first is None:  # all all-in, run it out
            self._run_out()

    def _run_out(self):
        while len(self.board) < 5:
            self.board += self.deck.draw(1)
        self.street = "complete"
        self.to_act = None

    # ---- payout ----
    def settle(self):
        """Distribute pot(s). Returns a human-readable result string."""
        # pull any uncollected street bets in
        self._collect()
        contenders = [p for p in self.players if p.in_hand]

        if len(contenders) == 1:
            w = contenders[0]
            w.chips += self.pot
            text = f"**{w.name}** wins {self.pot} (everyone else folded)."
            self.pot = 0
            self.in_progress = False
            self.last_winner_text = text
            return text

        # need a full board to evaluate
        while len(self.board) < 5:
            self.board += self.deck.draw(1)

        evaluator = Evaluator()
        # build side pots from committed amounts
        levels = sorted({p.committed for p in self.players if p.committed > 0})
        raw = []
        prev = 0
        for lvl in levels:
            contributors = [p for p in self.players if p.committed >= lvl]
            amount = (lvl - prev) * len(contributors)
            eligible = [p for p in contributors if p.in_hand]
            if amount > 0:
                raw.append((amount, eligible))
            prev = lvl
        # merge adjacent pots that have the same eligible players
        pots = []
        for amount, eligible in raw:
            key = frozenset(p.user_id for p in eligible)
            if pots and frozenset(x.user_id for x in pots[-1][1]) == key:
                pots[-1] = (pots[-1][0] + amount, pots[-1][1])
            else:
                pots.append((amount, eligible))

        results = []
        scores = {p.user_id: evaluator.evaluate(self.board, p.hole) for p in contenders}
        for amount, eligible in pots:
            if not eligible:
                continue
            best = min(scores[p.user_id] for p in eligible)
            winners = [p for p in eligible if scores[p.user_id] == best]
            share = amount // len(winners)
            rem = amount - share * len(winners)
            for i, w in enumerate(winners):
                w.chips += share + (1 if i < rem else 0)
            names = ", ".join(w.name for w in winners)
            cls = evaluator.get_rank_class(best)
            results.append(f"{names} win {amount} with {evaluator.class_to_string(cls)}")

        self.pot = 0
        self.in_progress = False
        text = "\n".join(results) if results else "No winners?"
        self.last_winner_text = text
        return text

    def hand_over(self):
        return self.street == "complete"


# ----------------------------------------------------------------------------
# DISCORD LAYER
# ----------------------------------------------------------------------------
import discord
from discord import app_commands
from discord.ext import commands

games = {}  # channel_id -> Game


def board_str(game):
    return "  ".join(card_str(c) for c in game.board) if game.board else "— no cards yet —"


def live_pot(game):
    return game.pot + sum(p.bet for p in game.players)


def table_embed(game, footer=None):
    e = discord.Embed(title="\u2660 Texas Hold'em", color=0x2ecc71)
    e.add_field(name="Board", value=board_str(game), inline=False)
    e.add_field(name="Pot", value=str(live_pot(game)), inline=True)
    e.add_field(name="Street", value=(game.street or "lobby").title(), inline=True)

    cur = game.current()
    lines = []
    for p in game.players:
        marker = "\u25B6\uFE0F " if cur is p else "\u2003"
        status = ""
        if not p.in_hand:
            status = " (folded)"
        elif p.all_in:
            status = " (all-in)"
        elif p.bet:
            status = f" (bet {p.bet})"
        lines.append(f"{marker}**{p.name}** — {p.chips} chips{status}")
    e.add_field(name="Players", value="\n".join(lines) or "—", inline=False)

    if cur and not game.hand_over():
        to_call = game.current_bet - cur.bet
        ask = f"{cur.name} to act" + (f" — {to_call} to call" if to_call > 0 else " — can check")
        e.set_footer(text=footer or ask)
    elif footer:
        e.set_footer(text=footer)
    return e


def cards_embed(player):
    e = discord.Embed(title="Your hole cards", color=0x3498db)
    e.description = "  ".join(card_str(c) for c in player.hole) or "no cards"
    return e


# ---- lobby ----
class LobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    def embed(self):
        names = "\n".join(f"• {p.name} ({p.chips} chips)" for p in self.game.players) or "nobody yet"
        e = discord.Embed(title="\u2660 Poker table — open", color=0xf1c40f,
                          description=f"**Players:**\n{names}\n\nBlinds {SMALL_BLIND}/{BIG_BLIND}. Hit Join, then Start.")
        return e

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(self, interaction, button):
        ok = self.game.add_player(interaction.user.id, interaction.user.display_name)
        msg = "Joined." if ok else "You're already in (or a hand is running)."
        await interaction.response.edit_message(embed=self.embed(), view=self)
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction, button):
        self.game.remove_player(interaction.user.id)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary)
    async def start(self, interaction, button):
        try:
            self.game.start_hand()
        except ValueError as err:
            await interaction.response.send_message(str(err), ephemeral=True)
            return
        view = ActionView(self.game)
        await interaction.response.edit_message(embed=table_embed(self.game), view=view)
        view.message = await interaction.original_response()


# ---- raise modal ----
class RaiseModal(discord.ui.Modal, title="Raise"):
    amount = discord.ui.TextInput(label="Raise the total bet to:", placeholder="e.g. 100")

    def __init__(self, game, view):
        super().__init__()
        self.game = game
        self.view = view

    async def on_submit(self, interaction):
        try:
            target = int(str(self.amount.value).strip())
            self.game.act(interaction.user.id, "raise", raise_to=target)
        except ValueError as err:
            await interaction.response.send_message(str(err) or "Enter a whole number.", ephemeral=True)
            return
        await self.view.after_action(interaction)


# ---- action view ----
class ActionView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game
        self.message = None
        self._build()

    def _build(self):
        self.clear_items()
        legal = self.game.legal_actions()
        self.fold_btn.disabled = "fold" not in legal
        self.add_item(self.fold_btn)
        if "check" in legal:
            self.check_call.label = "Check"
            self.check_call.style = discord.ButtonStyle.secondary
        else:
            self.check_call.label = f"Call {legal.get('call', '')}".strip()
            self.check_call.style = discord.ButtonStyle.primary
        self.check_call.disabled = not ("check" in legal or "call" in legal)
        self.add_item(self.check_call)
        self.raise_btn.disabled = "raise" not in legal
        self.add_item(self.raise_btn)
        self.add_item(self.cards_btn)

    async def _turn_guard(self, interaction):
        cur = self.game.current()
        if not cur or cur.user_id != interaction.user.id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return False
        return True

    async def after_action(self, interaction):
        if self.game.hand_over():
            result = self.game.settle()
            embed = table_embed(self.game, footer="Hand complete")
            embed.add_field(name="Result", value=result, inline=False)
            await interaction.response.edit_message(embed=embed, view=NextHandView(self.game))
        else:
            self._build()
            await interaction.response.edit_message(embed=table_embed(self.game), view=self)

    @discord.ui.button(label="Fold", style=discord.ButtonStyle.danger)
    async def fold_btn(self, interaction, button):
        if not await self._turn_guard(interaction):
            return
        self.game.act(interaction.user.id, "fold")
        await self.after_action(interaction)

    @discord.ui.button(label="Check", style=discord.ButtonStyle.secondary)
    async def check_call(self, interaction, button):
        if not await self._turn_guard(interaction):
            return
        legal = self.game.legal_actions()
        self.game.act(interaction.user.id, "check" if "check" in legal else "call")
        await self.after_action(interaction)

    @discord.ui.button(label="Raise", style=discord.ButtonStyle.primary)
    async def raise_btn(self, interaction, button):
        if not await self._turn_guard(interaction):
            return
        await interaction.response.send_modal(RaiseModal(self.game, self))

    @discord.ui.button(label="\U0001F440 My Cards", style=discord.ButtonStyle.secondary)
    async def cards_btn(self, interaction, button):
        p = self.game.get(interaction.user.id)
        if not p or not p.hole:
            await interaction.response.send_message("You're not holding any cards.", ephemeral=True)
            return
        await interaction.response.send_message(embed=cards_embed(p), ephemeral=True)


# ---- next hand ----
class NextHandView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label="Deal next hand", style=discord.ButtonStyle.success)
    async def deal(self, interaction, button):
        if len(self.game.seated()) < 2:
            await interaction.response.edit_message(
                embed=table_embed(self.game, footer="Not enough players with chips. Table closed."), view=None)
            games.pop(self.game.channel_id, None)
            return
        self.game.start_hand()
        view = ActionView(self.game)
        await interaction.response.edit_message(embed=table_embed(self.game), view=view)
        view.message = await interaction.original_response()

    @discord.ui.button(label="End table", style=discord.ButtonStyle.danger)
    async def end(self, interaction, button):
        games.pop(self.game.channel_id, None)
        await interaction.response.edit_message(
            embed=table_embed(self.game, footer="Table closed."), view=None)


# ---- bot ----
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.tree.command(description="Open a Texas Hold'em table in this channel.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def poker(interaction: discord.Interaction):
    cid = interaction.channel_id
    existing = games.get(cid)
    if existing and existing.in_progress:
        await interaction.response.send_message("A hand is already running here.", ephemeral=True)
        return
    game = Game(cid)
    games[cid] = game
    game.add_player(interaction.user.id, interaction.user.display_name)
    view = LobbyView(game)
    await interaction.response.send_message(embed=view.embed(), view=view)


@bot.event
async def setup_hook():
    await bot.tree.sync()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id}). Ready.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    bot.run(os.environ["DISCORD_BOT_TOKEN"])
