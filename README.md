# Poker Bot

A Discord bot that runs Texas Hold'em inside a server channel. Type `/poker`, people join,
and you play a real hand using buttons. Your hole cards stay private, while the board and pot
are shared with the table.

## Features

- Full Texas Hold'em: blinds, preflop/flop/turn/river, showdown
- Buttons for fold, check, call and raise (raise opens a box to set your total bet)
- Private hole cards behind a "My Cards" button that only you can see
- Hand ranking with proper side pots when players go all-in
- Chips tracked across hands for the session
- Multiple players per table, one table per channel

## How to play

Run `/poker` in any channel. A lobby card shows up. Players hit Join, then someone hits Start.
Check your hand with the My Cards button, and use Fold / Check / Call / Raise when it's your
turn. After the hand you can deal another one or close the table.

## Notes

- Chips reset when the bot restarts. There is no database yet.
- One table per channel at a time.
- Blinds are fixed at 10/20 and starting stacks at 1000.

Built with discord.py and treys.
