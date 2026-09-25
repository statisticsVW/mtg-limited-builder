# mtg-limited-builder

You've cracked six packs. There are ninety cards spread across the table, the clock is running, and
you're trying to decide whether blue-green is actually open or you just really want to play that one
rare. This project is an attempt to get you a second opinion.

**mtg-limited-builder** is a proof-of-concept [Claude Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
for sealed deckbuilding. It is experimental, and right now it only knows one set:
**Secrets of Strixhaven (SOS)**.

## What it does (the goal)

1. Lay out your sealed pool and take a few photos.
2. Hand them to Claude with the skill installed.
3. Get back a recommended 40-card deck.

That's the whole pitch. No typing card names into a spreadsheet and no scrolling through a
tier list with a pen in your mouth.

## How it works

Three steps, each doing the part it's good at:

- **Claude reads the photos.** It looks at your cards with vision and writes down what it thinks
  each one is. Photos are messy: glare, sleeves, a thumb over the name, a card half out of frame.
  So the list it produces is a rough draft.
- **A matcher cleans up the list.** Each rough name gets checked against every card that can show up
  in an SOS sealed pool. It shrugs off typical misreads, like `1` for `l` or `rn` for `m`, and
  fills in names that got cut off at the edge of a photo. It also knows that a two-faced card can
  be called by its front face alone. When a read is too close to call between two cards, it tells
  you instead of guessing, because slipping a card you don't own into your pool is worse than asking.
- **A builder picks the 40.** Using sealed win-rate data from 17Lands, it chooses your colors,
  your 23-ish spells, and your lands. *(Still to come; see below.)*

## Status

This is early. Here's what's real and what's still a sketch:

- [x] **Card pool data.** Every card that can appear in an SOS sealed pool: the main set, the
  Mystical Archive, and the Special Guests, 347 cards in all. Each is joined with 17Lands sealed
  performance data, with draft data filling in where the sealed sample is thin.
- [x] **Name matcher.** Turns noisy photo reads into real SOS cards, including counts like `4` or
  `2x`, and flags anything it can't confidently place. It's tested against a deliberately garbled
  pool list and comes back with zero wrong cards.
- [ ] **Deck builder.** Actually picking the 40 cards. Not started.
- [ ] **The skill itself.** The `SKILL.md` instructions and the zipped bundle you'd install in
  Claude. Not started.
- [ ] **More sets.** One format at a time.

## For tinkerers

Run the tests (no packages needed, and that's on purpose):

```bash
py -3.12 -m unittest discover -s tests -v
```

Everything that ships inside the skill is standard-library-only Python, since it runs in Claude's
sandbox where installing packages isn't a given. The one exception is `scripts/fetch_data.py`, which
rebuilds the card data on your own machine and needs the packages in `requirements-dev.txt`
(pandas, requests).

[CLAUDE.md](CLAUDE.md) has the repo layout, the rules, and notes on the data sources.

## Credits

- Card data from [Scryfall](https://scryfall.com).
- Performance data from [17Lands](https://www.17lands.com) public datasets.

mtg-limited-builder is unofficial fan content. It is not approved, endorsed, or sponsored by Wizards
of the Coast, and is not affiliated with Wizards of the Coast. Magic: The Gathering and all related
names are property of Wizards of the Coast LLC.
