"""Explainable headline rules for news impact scoring.

Calibrated on the live archive of 2026-11-15 (605 publisher stories in 72 h).
Before: every market story was "Low 15" because impact was scored before a
ticker was attached and general news was capped unless it matched a short
catalyst list; every ticker story with the word "earnings", "quarter",
"results" or "deal" anywhere in the headline was "High 65" (previews,
transcripts, valuation columns alike).

Two readings of a headline are scored and the stronger one wins:

* market reading  — base 15 + market-wide events (central-bank decision,
  CPI/PPI/jobs print, market stress, a major index/asset at a multi-week
  extreme, rate context);
* company reading — base 40 for a covered ticker or a bellwether company named
  in the headline, else 15, + company events (earnings result, guidance,
  CEO change, M&A, FDA decision, clinical data, a large stated price move,
  analyst action, legal/capital/executive/commercial items).

Filler (questions, listicles, comparisons, valuation columns, price
predictions, explainers, recaps, personal finance, promotion) is penalised on
both readings. Every rule that fires is returned as a short reason string.

The functions here are pure: headline in, evidence out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Tuple

# ── points ───────────────────────────────────────────────────────────────────
BASE_COVERED = 40
BASE_GENERAL = 15

PTS_CENTRAL_BANK_DECISION = 40
PTS_MACRO_PRINT = 40
PTS_MACRO_TIER2 = 10
PTS_MARKET_STRESS = 35
PTS_MARKET_EXTREME = 35
PTS_MARKET_SHARP_MOVE = 15
PTS_MARKET_LARGE_MOVE = 35
# What counts as large: an equity index moving 2%+ (or the Dow 500+ points),
# Bitcoin/Ether/oil/gold moving 8%+.
INDEX_LARGE_MOVE_PCT = 2.0
INDEX_SHARP_MOVE_PCT = 1.0
INDEX_LARGE_MOVE_POINTS = 500
INDEX_SHARP_MOVE_POINTS = 300
ASSET_LARGE_MOVE_PCT = 8.0
ASSET_SHARP_MOVE_PCT = 5.0
PTS_MARKET_EXTREME_AND_MOVE = 40
PTS_RATE_CONTEXT = 20

PTS_FDA_DECISION = 35
PTS_FDA_PENDING = 15
PTS_EARNINGS_RESULT = 25
PTS_EARNINGS_AND_GUIDANCE = 30
PTS_EARNINGS_FOLLOWUP = 5
PTS_CEO_CHANGE = 25
PTS_M_AND_A = 25
PTS_DISTRESS = 25
PTS_CLINICAL_DATA = 15
PTS_MOVE_10PCT = 20
PTS_MOVE_5PCT = 10
PTS_MOVE_RETROSPECTIVE = 5
PTS_PRICE_EXTREME = 10
PTS_VALUE_WIPEOUT = 15
PTS_MOVE_FAMILY_CAP = 25
PTS_ANALYST_ACTION = 10
PTS_SECONDARY = 8  # legal/regulatory, capital action, executive change, deal, restructuring

# Company events stack, but only an urgent event (FDA decision, wire
# "breaking") may lift a company story to CRITICAL.
COMPANY_BONUS_CAP = 39
MARKET_BONUS_CAP = 64
# A story about a company that is neither covered nor a bellwether stays
# below HIGH: it is not this user's market.
UNCOVERED_COMPANY_SCORE_CAP = 49

PENALTY_FILLER_FIRST = 20
PENALTY_FILLER_EXTRA = 5
PENALTY_FILLER_CAP = 30

_TRANSLATE = {
    0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',
    0x2013: "-", 0x2014: " - ", 0x00A0: " ", 0x2026: "...",
}


def normalize_headline(title: object) -> str:
    text = str(title or "").translate(_TRANSLATE).lower()
    return re.sub(r"\s+", " ", text).strip()


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# ── market-wide rules ────────────────────────────────────────────────────────
_CB = (
    r"(?:fed|fomc|federal reserve|ecb|european central bank|boe|bank of england|"
    r"boj|bank of japan|pboc|snb|rba|central bank)"
)
CENTRAL_BANK_DECISION_RE = _rx(
    rf"\b{_CB}\b[^.;:|?]{{0,40}}?\b(?:raises?|raised|hikes?|hiked|cuts?|lowers?|lowered|slashes?|"
    rf"holds?|held|keeps?|kept|leaves?|left|pauses?|paused)\b[^.;:|?]{{0,25}}?"
    rf"(?:\brates?\b|\binterest\b|\bsteady\b|\bunchanged\b|\bby \d|\d ?(?:bps?|basis points?)\b)"
    rf"|\b{_CB} (?:announces|decision|statement|raises|cuts|hikes|holds|hold|pivot)\b"
    r"|\b(?:rate|interest rate|policy) decision\b"
    r"|\bemergency rate\b|\braises rates\b|\bcuts rates\b|\bhikes rates\b"
    r"|\brate (?:hike|cut) of \d"
)
MACRO_PRINT_RE = _rx(
    r"\b(?:cpi|ppi|pce|core inflation|consumer prices?|producer prices?|consumer price index|"
    r"producer price index|inflation (?:report|data|print|reading|figures?|numbers?|rate)|"
    r"jobs report|jobs data|non-?farm payrolls?|nfp|payrolls (?:report|data)|unemployment rate)\b"
)
# A print that leads the headline is the report itself; later in the headline
# it is a reaction story ("Stocks slip despite a cooler PPI print").
MACRO_PRINT_LEAD_CHARS = 20
# "CPI Aerostructures to Report Third Quarter Results", "PPI Corp ...": a
# company whose name is a macro acronym.
MACRO_ACRONYM_COMPANY_RE = _rx(
    r"\b(?:cpi|ppi|pce|nfp) (?:aerostructures|aero|card|corp|corporation|inc|group|holdings|"
    r"property|technologies|energy|industries)\b"
    r"|\b(?:quarter|quarterly|q[1-4]|fiscal) (?:\d{4} )?(?:financial )?(?:results|earnings)\b"
)
# The event has not happened yet ("traders brace for Fed decision", "ahead of
# CPI"): context, not the event.
MACRO_ANTICIPATION_RE = _rx(
    r"\b(?:braces? for|braced for|bracing for|ahead of|awaits?|awaiting|waits? for|waiting for|"
    r"looms?|looming|eyes|eyeing|countdown to|preview|what to expect|bets on|odds of|due (?:on|this|next|"
    r"monday|tuesday|wednesday|thursday|friday)|this week's|next week's|upcoming)\b"
)
MACRO_TIER2_RE = _rx(
    r"\b(?:gdp|gross domestic|retail sales|housing starts|building permits|pmi|manufacturing index|"
    r"jobless claims|trade deficit|consumer confidence|consumer sentiment|durable goods)\b"
)
MARKET_STRESS_RE = _rx(
    r"\b(?:trading halt(?:ed|s)?|trading (?:is |was )?halted|halts trading|circuit breakers?|"
    r"flash crash|market crash|stock market crash|banking crisis|bank run|systemic risk|contagion|"
    r"black swan|sovereign default|liquidity crisis|credit crunch|market meltdown)\b"
)
RATE_CONTEXT_RE = _rx(
    r"\b(?:fed(?! up\b)|fomc|federal reserve|central banks?|ecb|boe|boj|rate (?:hike|cut)s?|"
    r"interest[- ]rates?|rate (?:fears|outlook|expectations|path|bets)|treasury yields?|bond yields?|"
    r"10[- ]year (?:yield|treasury)|yield curve|inflation (?:fears|worries|concerns|risk|surge)|"
    r"hawkish|dovish|monetary policy|fed minutes|powell speech|treasury secretary|risk-off|"
    r"flight to safety|vix spike|volatility surge|global sell-?off|dollar index|recession|"
    r"stagflation|debt ceiling|government shutdown)\b"
)
_POWELL_RE = _rx(r"\bpowell\b")
_FED_CONTEXT_RE = _rx(r"\b(?:fed|fomc|federal reserve|central bank|chair|rates?|monetary|jerome)\b")

_MAJOR_ASSET = (
    r"(?:bitcoin|btc|ethereum|ether|eth|crypto(?:currency)? markets?|crypto|stocks|stock market|"
    r"equities|wall street|s&p ?500|s&p|nasdaq|dow jones|dow|russell 2000|treasur(?:y|ies)|"
    r"bond market|dollar index|dollar|oil|crude|brent|wti|gold|vix|nikkei|ftse|dax|hang seng|"
    r"global markets|markets)"
)
MAJOR_ASSET_RE = _rx(rf"\b{_MAJOR_ASSET}\b")
_PERIOD_WORD = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|multi)"
EXTREME_RE = _rx(
    rf"\b{_PERIOD_WORD}[- ](?:week|month|year|decade)s? (?:low|high)s?\b"
    r"|\b52[- ]week (?:low|high)s?\b"
    r"|\brecord (?:high|low|close)s?\b|\ball[- ]time (?:high|low)s?\b"
    r"|\b(?:lowest|highest)(?: level| close| point)? (?:since|in (?:\w+ )?(?:weeks|months|years))\b"
    r"|\blongest (?:\w+ )?(?:losing|winning) streak\b"
    r"|\b(?:worst|best) (?:day|week|month|quarter|year) (?:since|in)\b"
    r"|\bcorrection territory\b|\bbear market territory\b|\bnew (?:high|low)s?\b"
)
SHARP_MOVE_RE = _rx(
    r"\b(?:crash(?:es|ed|ing)?|plunge[sd]?|plunging|plummet(?:s|ed|ing)?|tumble[sd]?|tumbling|"
    r"tank(?:s|ed)?|sell-?off|rout|slump(?:s|ed)?|nosedive[sd]?|collapse[sd]?|meltdown|"
    r"wiped out|bloodbath|capitulat\w+|soar(?:s|ed|ing)?|surge[sd]?|surging|skyrocket(?:s|ed)?)\b"
)
_EQUITY_INDEX = (
    r"(?:s&p ?500|s&p|nasdaq|dow jones|dow|russell 2000|nikkei|ftse|dax|hang seng|stoxx ?\d*)"
)
EQUITY_INDEX_RE = _rx(rf"\b{_EQUITY_INDEX}\b")
_INDEX_MOVE_VERB = (
    r"(?:jumps?|jumped|soars?|soared|surges?|surged|rallies|rallied|gains?|gained|climbs?|climbed|"
    r"rises?|rose|adds?|added|up|higher|sinks?|sank|plunges?|plunged|plummets?|tumbles?|tumbled|"
    r"falls?|fell|drops?|dropped|slides?|slid|slumps?|slumped|crash(?:es|ed)?|sheds?|tanks?|tanked|"
    r"dives?|dived|down|lower|declines?|declined|dips?|dipped|loses?|lost|retreats?|closes? (?:up|down)|"
    r"closed (?:up|down))"
)
# The percentage / points figure that belongs to the index itself, stated as a
# move ("S&P 500 falls 2.3%", "Dow drops 900 points").
INDEX_MOVE_PCT_RE = _rx(
    rf"\b{_EQUITY_INDEX}\b[^.;|,%]{{0,30}}?\b{_INDEX_MOVE_VERB}\b[^.;|,%]{{0,20}}?(\d+(?:\.\d+)?) ?%"
)
INDEX_MOVE_POINTS_RE = _rx(
    rf"\b{_EQUITY_INDEX}\b[^.;|%]{{0,30}}?\b{_INDEX_MOVE_VERB}\b[^.;|%]{{0,20}}?(\d{{3,4}}|\d,\d{{3}}) points?\b"
)
# A named asset with its own move ("Bitcoin drops 9%"). A generic subject
# ("stocks slip as Nvidia falls 8%") has no figure of its own, and a
# percentage that is not a move ("Ethereum project slashes staff by 20%")
# does not count.
_NAMED_ASSET = r"(?:bitcoin|btc|ethereum|ether|eth|oil|crude|brent|wti|gold|dollar index|vix)"
NAMED_ASSET_MOVE_PCT_RE = _rx(
    rf"\b{_NAMED_ASSET}\b[^.;|,%]{{0,30}}?\b{_INDEX_MOVE_VERB}\b[^.;|,%]{{0,20}}?(\d+(?:\.\d+)?) ?%"
)
VALUE_WIPEOUT_RE = _rx(
    r"\b(?:lost|loses|losing|wipes? out|wiped out|erases?|erased|sheds?|shed|sinks? by|falls? by|"
    r"drops? by)\b[^.;|?]{0,25}\$[\d.,]+ ?(?:trillion|billion|bn|tn|b|t)\b"
    r"|\bmarket (?:value|cap)\b[^.;|?]{0,30}\b(?:sinks?|falls?|drops?|plunges?|slumps?|wiped|erased)\b"
)

# ── company rules ────────────────────────────────────────────────────────────
FDA_DECISION_RE = _rx(
    r"\bfda (?:approves|approved|approval|clears|cleared|clearance|rejects|rejected|rejection|"
    r"reject|denial|grants|granted)\b|\bclinical hold\b|\bcomplete response letter\b|"
    r"\bbreakthrough therapy\b|\bfast track\b|\bpriority review\b"
)
FDA_PENDING_RE = _rx(
    r"\bfda (?:decision|approval|ruling|action|filing|review|submission)\b[^.;|?]{0,25}"
    r"\b(?:nears|looms|awaits|awaited|expected|due|date|ahead|pending|before)\b"
    r"|\b(?:awaits|ahead of|before|pending|seeks|wants|files for|filing for|submits)\b[^.;|?]{0,30}\bfda\b"
    r"|\bpdufa\b"
)
EARNINGS_PREVIEW_RE = _rx(
    r"\b(?:to|will) (?:report|host|release|announce|provide|post|hold|webcast|present)\b"
    r"|\b(?:ahead of|before|pre-?)(?: its| the)? ?(?:q[1-4] |quarterly |\w+ \d+ )?(?:earnings|results)\b"
    r"|\bearnings (?:ahead|preview|on deck|to watch|looms?|date|hope|focus)\b"
    r"|\bwhat(?:'s| is) in (?:store|the offing)\b|\bwhat to (?:expect|watch)\b|\bgears up\b"
    r"|\bannounces (?:date|timing)\b|\b(?:date|schedules?|scheduled|re-?schedules?)\b[^.;|?]{0,40}"
    r"\b(?:conference call|earnings|results|financial update)\b"
    r"|\bexpected to (?:beat|report|post|miss)\b|\bseeking clues\b|\bpeek into\b"
    r"|\bwall street projections\b|\banalyst projections\b|\bnears earnings\b"
    r"|\bfaces q[1-4] results\b|\bpre-q[1-4]\b|\bsurvive earnings\b|\bearnings ahead\b"
)
EARNINGS_FOLLOWUP_RE = _rx(
    r"\bearnings call\b|\bcall (?:transcript|highlights|summary)\b|\btranscript\b|\bkey metrics\b"
    r"|\bearnings analysis\b|\bquestions answered\b|\btakeaways\b|\bsaid on its\b"
)
EARNINGS_RESULT_RE = _rx(
    r"\b(?<!to )(?:beats?|tops?|topped|miss(?:es|ed)?|meets?|exceeds?|exceeded|surpass(?:es|ed)?|"
    r"crush(?:es|ed)?|falls? short of)\b[^.;|?]{0,40}\b(?:estimates?|expectations?|forecasts?|"
    r"consensus|earnings|revenues?|sales|profit|eps|guidance|the street)\b"
    r"|\bearnings (?:beat|miss|surprise|snapshot)\b"
    r"|\b(?:expectations?|estimates?|guidance) (?:miss|beat)\b"
    r"|\b(?:earnings|revenues?|sales|profit|eps) (?:beats?|miss(?:es)?|tops?)\b"
    r"|\b(?:reports?|reported|posts?|posted|announces?|announced|delivers?|delivered|releases?|"
    r"released)(?=\s)[^.;|?]{0,60}\b(?:results|earnings|profit|loss|net income|revenues?|ebitda)\b"
    r"|\b(?:q[1-4]|(?:first|second|third|fourth)[- ]quarter|quarterly|full[- ]year)"
    r"(?: \d{4})? (?:adjusted )?(?:results|earnings|profit|loss|revenues?|net income|ebitda)\b"
    r"[^.;|?]{0,40}\b(?:falls?|fell|rises?|rose|jumps?|jumped|drops?|dropped|grows?|grew|surges?|"
    r"slumps?|declines?|doubles?|turns?)\b"
    r"|\bswings? to (?:a )?(?:\$[\d.,]+ ?\w* )?(?:loss|profit)\b|\bswung to\b"
    r"|\brecord (?:\$[\d.,]+ ?\w+ )?(?:quarterly |annual |q[1-4] )?(?:profit|revenues?|earnings|sales|net income)\b"
    r"|\b(?:profit|revenues?|earnings|net income|sales|ebitda) (?:jumps?|jumped|falls?|fell|rises?|"
    r"rose|drops?|surges?|slumps?|doubles?|plunges?|soars?|grew|grows|climbs?|declines?|"
    r"turns? negative|hits? a record)\b"
    r"|\bearnings (?:report|results?) (?:released|published|out)\b"
    r"|\b(?:after|on|following|despite|as) (?:its |a |the )?(?:(?:record|strong|weak|mixed|solid|"
    r"upbeat|q[1-4]|quarterly|(?:first|second|third|fourth)[- ]quarter) ){0,3}"
    r"(?:earnings|results|beat)\b"
    r"|\b(?:q[1-4]|(?:first|second|third|fourth)[- ]quarter) (?:beat|miss|loss|profit)\b"
)
GUIDANCE_RE = _rx(
    r"\b(?:raises?|raised|lifts?|lifted|boosts?|hikes?|cuts?|lowers?|lowered|slashes?|trims?|"
    r"reaffirms?|reiterates?|withdraws?|suspends?|narrows?|higher|lower|soft|weak|upbeat|"
    r"downbeat|disappointing)\b[^.;|?]{0,30}\b(?:guidance|outlook|forecast)\b"
    r"|\b(?:guidance|outlook|forecast) (?:cuts?|raise|raised|hike|reset|below|above|miss|"
    r"disappoints?|tops?)\b"
)
CEO_CHANGE_RE = _rx(
    r"\b(?:ceo|chief executive|chairman|chairwoman)\b[^.;|?]{0,40}\b(?:resigns?|resigned|"
    r"steps? down|stepping down|stepped down|ousted|ouster|ousting|fired|departs?|departure|"
    r"to retire|retires?|named|appointed|replaced|dies)\b"
    r"|\b(?:resigns?|resigned|steps? down|stepped down|ousted|fired|out|exits?|retires?)\b"
    r"[^.;|?]{0,12}\bas\b[^.;|?]{0,30}\b(?:ceo|chief executive|chairman)\b"
    r"|\b(?:names?|named|appoints?|appointed|taps?|hires?)\b[^.;|?]{0,50}?"
    r"\b(?:as )?(?:its )?(?:new |next |interim )?(?:ceo|chief executive)\b"
    r"|\b(?:new|interim|next) (?:ceo|chief executive)\b"
    r"|\bceo (?:succession|exit|departure|resignation|search|shake-?up|ouster)\b"
)
M_AND_A_RE = _rx(
    r"\b(?:to acquire|acquires|acquired by|acquiring|acquisition of|to be acquired|"
    r"agrees? to (?:buy|acquire|sell|merge)|merger (?:talks|agreement|deal|with)|merge with|"
    r"to merge|takeover|buyout|take[- ]private|tender offer|purchase offer|"
    r"(?:in|holds?|reported) talks? (?:to|over|with)|"
    r"explor(?:es|ing) (?:a )?(?:sale|acquisition|merger|strategic alternatives)|"
    r"strategic alternatives|hostile bid|spin[- ]?off|spins? off|share purchase agreement|"
    r"(?:could|may|might) buy)\b"
)
DISTRESS_RE = _rx(
    r"\b(?:bankruptcy|chapter 11|going concern|delist(?:ed|ing|s)?|defaults? on|insolven\w+)\b"
)
CLINICAL_RE = _rx(
    r"\bphase (?:1|2|3|i|ii|iii|1b|2a|2b)\b|\btrial (?:results?|data|success|failure|fails?|meets?)\b"
    r"|\bin trial\b|\btopline\b|\bprimary endpoint\b|\bclinical (?:setbacks?|data|results)\b"
    r"|\blong[- ]term data\b"
)
_MOVE_VERB = (
    r"(?:jumps?|jumped|soars?|soared|surges?|surged|rockets?|rallies|rallied|rally|gains?|gained|"
    r"climbs?|climbed|rises?|rose|pops?|popped|spikes?|spiked|leaps?|up|higher|sinks?|sank|"
    r"plunges?|plunged|plummets?|tumbles?|tumbled|falls?|fell|drops?|dropped|slides?|slid|"
    r"slumps?|slumped|crash(?:es|ed)?|sheds?|tanks?|tanked|dives?|dived|down|lower|declines?|"
    r"declined|dips?|dipped|loses?|lost|retreats?|soaring|surging|plunging|tumbling)"
)
MOVE_PCT_RE = _rx(
    rf"\b{_MOVE_VERB}\b(?: (?:nearly|almost|over|more than|about|around|by|as much as|another|"
    rf"roughly|a))* ?(\d+(?:\.\d+)?) ?%"
)
PCT_MOVE_NOUN_RE = _rx(
    r"(\d+(?:\.\d+)?) ?% (?:drop|fall|decline|plunge|jump|gain|rally|surge|rise|slide|"
    r"sell-?off|loss|pop|crash|slump)\b"
    r"(?! in (?:\w+ ){0,3}(?:holdings|stake|revenues?|sales|profit|earnings|ebitda|income|"
    r"deliveries|volumes?|q[1-4]|orders|bookings))"
)
# "Revenue jumped 50%", "trading volume falls 21%": a business metric moved.
METRIC_SUBJECT_RE = _rx(
    r"\b(?:revenues?|sales|profit|profits|earnings|income|ebitda|eps|deliveries|volumes?|orders|"
    r"margins?|stake|holdings|positions|staff|workforce|output|production|users|subscribers|"
    r"bookings|backlog|dividends?|payout|rents?|reserves?|supply|inflows?|outflows?|growth)"
    r"(?: (?:is|are|was|were|has|have))?\s*$"
)
# The extreme is about a non-price series ("exchange reserves hit a five-year low").
NON_PRICE_SUBJECT_RE = _rx(
    r"\b(?:reserves?|supply|inflows?|outflows?|volumes?|dominance|premium|fear and greed|"
    r"funding rates?|open interest|hashrate|sales|deliveries|sentiment)\b"
)
DOUBLE_DIGIT_RE = _rx(r"\bdouble[- ]digits?\b")
RETROSPECTIVE_RE = _rx(
    r"\bin (?:a|the past|the last|one|two|three|\d+) (?:week|month|year|days|weeks|months|years)\b"
    r"|\bthis (?:year|month|week)\b|\byear[- ]to[- ]date\b|\bytd\b|\b(?:\d+|three|five|ten)[- ]?year run\b"
    r"|\bafter (?:a|its) \d+% (?:run|rise|rally)\b|\b\d+% (?:run|rise)\b|\bover (?:the )?(?:past|last)\b"
    r"|\bweek ended\b|\bsince (?:19|20)\d\d\b|\ba year\b|\bover \w+ decades\b"
    r"|\bfrom last (?:year|winter|summer|spring|fall|month|week)\b|\byear[- ]over[- ]year\b|\byoy\b"
    r"|\bwithout a\b|\bhas gone \d+\b"
)
ANALYST_ACTION_RE = _rx(
    r"\b(?:upgrades|downgrades)\b(?=[^.;|?]{0,60}\b(?:to|from|stock|shares|rating|buy|sell|hold|"
    r"neutral|outperform|overweight|underweight|underperform)\b)"
    r"|\b(?:upgraded|downgraded) [a-z]"
    r"|\b\w+'s (?:upgrade|downgrade)\b|\banalyst (?:upgrade|downgrade)s?\b"
    r"|\b(?:price target|target price|initiates? coverage|"
    r"(?:raises?|lifts?|cuts?|lowers?|boosts?|trims?|hikes?) (?:\w+ ){0,4}(?:rating|targets?|pt)|"
    r"rating (?:cut|raised|upgrade|downgrade)|overweight|underweight|outperform|underperform|"
    r"buy rating|sell rating|\$\d[\d.,]* (?:price )?target)\b"
)
LEGAL_RE = _rx(
    r"\b(?:doj|justice department|sec (?:charges|sues|probe|investigation|subpoena)|ftc|antitrust|"
    r"lawsuits?|sued|sues|suing|probe|investigation|subpoena|indict\w*|fraud|scandal|smuggling|"
    r"recall(?:s|ed)?|fined|fine|settles?|settlement|class action|sanction(?:s|ed)?|"
    r"export (?:curbs|controls|rules|restrictions)|chip (?:curbs|rules)|patent infringement|"
    r"data breach|data leak|hack(?:ed|s)?|cyberattack|outage)\b"
)
CAPITAL_RE = _rx(
    r"\b(?:public offering|secondary offering|stock offering|share offering|share sale|"
    r"equity (?:raise|offering)|raises? \$[\d.,]+ ?(?:m|b|bn|million|billion)|convertible (?:notes|offering)|"
    r"private placement|buyback|share repurchase|repurchase program|special (?:cash )?dividend|"
    r"dividend (?:hike|cut|increase|suspension)|stock split|reverse split|debuts? below|"
    r"prices? (?:its )?ipo|files? for (?:an )?ipo|index removal|index inclusion|"
    r"(?:removed|dropped|booted) from (?:the )?(?:s&p|nasdaq|msci|russell|dow))\b"
)
EXEC_CHANGE_RE = _rx(
    r"\b(?:cfo|coo|cto|chief financial officer|chief operating officer|chief \w+ officer|evp)\b"
    r"|\bleadership (?:transition|change|shake-?up|handover|evolution|shock|crisis)\b"
    r"|\bfinance (?:leadership )?(?:reshuffle|shake-?up)\b|\bexecutive leadership\b"
)
DEAL_RE = _rx(
    r"\b(?:partners? with|partnership|teams? up with|"
    r"(?:signs?|signed|wins?|won|secures?|lands?|inks?|awarded)\b[^.;|?]{0,50}\b(?:deal|contract|agreement|order|pact)|"
    r"\$[\d.,]+ ?(?:billion|million|bn|b|m) (?:in )?(?:\w+ ){0,3}contracts?|"
    r"(?:on|after|following) \w+ deal)\b"
)
RESTRUCTURING_RE = _rx(
    r"\b(?:layoffs?|lays? off|job cuts|(?:cuts?|slashes?) (?:\d+% of )?(?:its )?(?:staff|workforce|jobs)|"
    r"restructur\w+|workforce (?:cuts?|reduction)|winds? down|shut(?:s|ting)? down)\b"
)

# ── filler ───────────────────────────────────────────────────────────────────
FILLER_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("question headline", _rx(r"\?")),
    ("listicle", _rx(
        r"^\d+ (?:\w+ ){0,3}(?:reasons|stocks|things|ways|signs|takeaways|picks|ideas|charts|"
        r"lessons|questions|habits|catalysts|developments|ais?)\b|\btop \d+\b"
        r"|\b\d+ (?:key |big |top |best |intriguing )?(?:reasons|stocks to|key takeaways|takeaways|"
        r"signs|things|ais|habits|catalysts|developments)\b"
        r"|\b(?:stocks|cryptos?|coins) to (?:buy|watch|sell|avoid)\b|\bmust[- ]watch\b"
    )),
    ("comparison column", _rx(
        r"\bvs\.?(?= )|\bversus\b|\bbetter (?:buy|value|dividend|stock)\b|\bwhich (?:\w+ ){0,4}(?:stock|is better)\b"
    )),
    ("valuation commentary", _rx(
        r"\b(?:undervalued|overvalued|fully valued|fully priced|fair value|pricey|too expensive|"
        r"bargain|attractively priced|valuation (?:test|debate|check)|buy now|hold or fold|"
        r"should you (?:buy|invest|sell)|trending stock|bull case|bull of the day|bear of the day|"
        r"bull and bear|strong (?:buy|sell) stocks|zacks|worth buying|still a buy|looks stretched|"
        r"looks about right|priced like|time to buy|investment story|investment narrative|"
        r"how to play|what you should know|facts to know|something to think about|"
        r"what history says|history says|research reports|analyst blog|investment ideas|"
        r"long-term investors|value investors|a buy\b)\b"
    )),
    ("price prediction", _rx(
        r"\b(?:price predictions?|price analysis|prediction:|predictions|predicts|price forecast|"
        r"could (?:hit|reach|fall|climb|drop|soar|crash|go|cost)|how (?:low|high|much higher|far) can|"
        r"where will|will be worth|next stop|back in play|what's next|what comes next|"
        r"happens next|where is the bottom|brutal targets|key level to watch|we asked)\b"
    )),
    ("explainer or opinion", _rx(
        r"^(?:why|how|what|who|where|when|here's|here is|this|these|meet|inside|forget|if|"
        r"opinion|analysis)\b|\bexplained\b|\bcolumnist\b|\bargues\b"
        r"|\bhere's (?:what|how|why)\b|\bwhat it means\b|\bwhat to know\b|\boffer a blueprint\b"
    )),
    ("recap column", _rx(
        r"\b(?:weekly recap|week in review|weekend watch|morning minute|live updates|bits recap|"
        r"daily recap|asia express|happened in crypto today|stock movers|market wrap|week ahead|"
        r"what to watch|crypto recap|recap)\b"
    )),
    ("personal finance or lifestyle", _rx(
        r"\b(?:my (?:husband|wife|mother|father|partner|kids|son|daughter)|i'm \d+|i am \d+|"
        r"i inherited|i paid|retirees?|retirement (?:account|savings|plan|income|age)|"
        r"(?:your|in|for|a|afford) (?:\S+ )?retirement|social security|should i|am i|was i|"
        r"your (?:portfolio|investing|money|taxes)|save on taxes|digital nomad|art market|"
        r"a widow)\b|\b401\(k\)"
    )),
    ("promotion", _rx(
        r"\b(?:to (?:present|participate|exhibit|showcase)|participation in|investor conference|"
        r"named to|trailblazer|webinar|free bitcoin|cloud mining|beyond your imagination|"
        r"exhibition debut|best companies list)\b"
    )),
)


@dataclass
class HeadlineEvidence:
    """What the rules found in one headline."""

    market_points: int = 0
    market_reasons: List[str] = field(default_factory=list)
    company_points: int = 0
    company_reasons: List[str] = field(default_factory=list)
    filler_penalty: int = 0
    filler_reasons: List[str] = field(default_factory=list)
    macro_tier: int = 0          # 1 = decision or leading print, 2 = second-tier print
    urgent: bool = False         # decision, leading macro print, market stress, FDA decision
    market_catalyst: bool = False
    fda_decision: bool = False
    largest_move_pct: Optional[float] = None


def _add(reasons: List[str], label: str, points: int) -> int:
    reasons.append(f"{label} ({points:+d})")
    return points


def mentions_fed_chair_powell(text: str) -> bool:
    return bool(_POWELL_RE.search(text) and _FED_CONTEXT_RE.search(text))


def largest_stated_move(text: str) -> Optional[float]:
    values: List[float] = []
    for rx in (MOVE_PCT_RE, PCT_MOVE_NOUN_RE):
        for match in rx.finditer(text):
            if rx is MOVE_PCT_RE and METRIC_SUBJECT_RE.search(text[max(0, match.start() - 40):match.start()]):
                continue
            try:
                values.append(float(match.group(1)))
            except (TypeError, ValueError):
                continue
    if DOUBLE_DIGIT_RE.search(text):
        values.append(10.0)
    values = [v for v in values if 0 < v <= 1000]
    return max(values) if values else None


def evaluate_headline(title: object) -> HeadlineEvidence:
    """Evidence for one headline. The result is cached: treat it as read-only."""
    return _evaluate_cached(str(title or ""))


@lru_cache(maxsize=8192)
def _evaluate_cached(title: str) -> HeadlineEvidence:
    text = normalize_headline(title)
    ev = HeadlineEvidence()
    if not text:
        return ev

    # ── market reading ──────────────────────────────────────────────────────
    decision = bool(CENTRAL_BANK_DECISION_RE.search(text)) or (
        mentions_fed_chair_powell(text) and bool(re.search(r"\brates?\b", text))
    )
    print_match = MACRO_PRINT_RE.search(text)
    if print_match and MACRO_ACRONYM_COMPANY_RE.search(text):
        print_match = None
    tier2 = bool(MACRO_TIER2_RE.search(text))
    market = 0
    upcoming = (decision or bool(print_match)) and bool(MACRO_ANTICIPATION_RE.search(text))
    if upcoming:
        market += _add(ev.market_reasons, "upcoming macro event (not yet out)", PTS_RATE_CONTEXT)
        decision = False
        print_match = None
    if decision:
        market += _add(ev.market_reasons, "central-bank rate decision", PTS_CENTRAL_BANK_DECISION)
        ev.macro_tier = 1
        ev.urgent = True
    if print_match:
        leading = print_match.start() <= MACRO_PRINT_LEAD_CHARS
        if not decision:
            label = "inflation/jobs print" if leading else "reaction to an inflation/jobs print"
            market += _add(ev.market_reasons, label, PTS_MACRO_PRINT)
        if leading:
            ev.macro_tier = 1
            ev.urgent = True
    if tier2 and not decision and not print_match:
        market += _add(ev.market_reasons, "second-tier macro data", PTS_MACRO_TIER2)
        ev.macro_tier = 2
    if MARKET_STRESS_RE.search(text):
        market += _add(ev.market_reasons, "market stress event", PTS_MARKET_STRESS)
        ev.urgent = True
    if not upcoming and not decision and not print_match and (
        RATE_CONTEXT_RE.search(text) or mentions_fed_chair_powell(text)
    ):
        market += _add(ev.market_reasons, "rates / central-bank context", PTS_RATE_CONTEXT)

    move_pct = largest_stated_move(text)
    ev.largest_move_pct = move_pct
    retrospective = bool(RETROSPECTIVE_RE.search(text))
    non_price = bool(NON_PRICE_SUBJECT_RE.search(text))
    if MAJOR_ASSET_RE.search(text):
        extreme = bool(EXTREME_RE.search(text)) and not non_price
        large = False
        sharp = bool(SHARP_MOVE_RE.search(text) or VALUE_WIPEOUT_RE.search(text))
        if EQUITY_INDEX_RE.search(text):
            index_pct = max((float(m.group(1)) for m in INDEX_MOVE_PCT_RE.finditer(text)), default=0.0)
            index_points = max(
                (float(m.group(1).replace(",", "")) for m in INDEX_MOVE_POINTS_RE.finditer(text)), default=0.0
            )
            if not retrospective:
                large = index_pct >= INDEX_LARGE_MOVE_PCT or index_points >= INDEX_LARGE_MOVE_POINTS
                sharp = sharp or index_pct >= INDEX_SHARP_MOVE_PCT or index_points >= INDEX_SHARP_MOVE_POINTS
        elif not retrospective and not non_price:
            asset_pct = max((float(m.group(1)) for m in NAMED_ASSET_MOVE_PCT_RE.finditer(text)), default=0.0)
            large = asset_pct >= ASSET_LARGE_MOVE_PCT
            sharp = sharp or asset_pct >= ASSET_SHARP_MOVE_PCT
        if extreme and (sharp or large):
            market += _add(ev.market_reasons, "major index/asset at a multi-week extreme, sharp move",
                           PTS_MARKET_EXTREME_AND_MOVE)
        elif extreme:
            market += _add(ev.market_reasons, "major index/asset at a multi-week extreme", PTS_MARKET_EXTREME)
        elif large:
            market += _add(ev.market_reasons, "large move in a major index/asset", PTS_MARKET_LARGE_MOVE)
        elif sharp:
            market += _add(ev.market_reasons, "sharp move in a major index/asset", PTS_MARKET_SHARP_MOVE)
    if market > MARKET_BONUS_CAP and not ev.urgent:
        ev.market_reasons.append(f"market events capped at +{MARKET_BONUS_CAP}")
        market = MARKET_BONUS_CAP
    ev.market_points = market
    ev.market_catalyst = market > 0 and (
        decision or bool(print_match) or bool(MARKET_STRESS_RE.search(text))
        or any("major index/asset" in r or "rates /" in r for r in ev.market_reasons)
    )

    # ── company reading ─────────────────────────────────────────────────────
    company = 0
    macro_headline = decision or bool(print_match) or tier2
    if FDA_DECISION_RE.search(text) and not FDA_PENDING_RE.search(text):
        company += _add(ev.company_reasons, "FDA decision", PTS_FDA_DECISION)
        ev.fda_decision = True
        ev.urgent = True
    elif FDA_PENDING_RE.search(text):
        company += _add(ev.company_reasons, "FDA decision pending", PTS_FDA_PENDING)

    preview = bool(EARNINGS_PREVIEW_RE.search(text))
    followup = bool(EARNINGS_FOLLOWUP_RE.search(text))
    result = bool(EARNINGS_RESULT_RE.search(text)) and not macro_headline
    guidance = bool(GUIDANCE_RE.search(text))
    if preview and (result or guidance or "earnings" in text or "results" in text):
        ev.company_reasons.append("earnings preview or scheduling (no event bonus)")
    elif followup and (result or guidance or "earnings" in text):
        company += _add(ev.company_reasons, "earnings follow-up coverage", PTS_EARNINGS_FOLLOWUP)
    elif result and guidance:
        company += _add(ev.company_reasons, "earnings result with guidance change", PTS_EARNINGS_AND_GUIDANCE)
    elif result:
        company += _add(ev.company_reasons, "earnings result", PTS_EARNINGS_RESULT)
    elif guidance:
        company += _add(ev.company_reasons, "guidance change", PTS_EARNINGS_RESULT)

    if CEO_CHANGE_RE.search(text):
        company += _add(ev.company_reasons, "CEO change", PTS_CEO_CHANGE)
    elif EXEC_CHANGE_RE.search(text):
        company += _add(ev.company_reasons, "executive change", PTS_SECONDARY)
    if M_AND_A_RE.search(text):
        company += _add(ev.company_reasons, "M&A", PTS_M_AND_A)
    elif DEAL_RE.search(text):
        company += _add(ev.company_reasons, "commercial deal", PTS_SECONDARY)
    if DISTRESS_RE.search(text):
        company += _add(ev.company_reasons, "financial distress", PTS_DISTRESS)
    if CLINICAL_RE.search(text):
        company += _add(ev.company_reasons, "clinical data", PTS_CLINICAL_DATA)

    move_points = 0
    move_reasons: List[str] = []
    if move_pct is not None and move_pct >= 5:
        if retrospective:
            move_points += _add(move_reasons, f"{move_pct:g}% move over a longer period", PTS_MOVE_RETROSPECTIVE)
        elif move_pct >= 10:
            move_points += _add(move_reasons, f"{move_pct:g}% price move", PTS_MOVE_10PCT)
        else:
            move_points += _add(move_reasons, f"{move_pct:g}% price move", PTS_MOVE_5PCT)
    if EXTREME_RE.search(text) and not non_price:
        move_points += _add(move_reasons, "price at a multi-week extreme", PTS_PRICE_EXTREME)
    if VALUE_WIPEOUT_RE.search(text):
        move_points += _add(move_reasons, "large market-value swing", PTS_VALUE_WIPEOUT)
    if move_points > PTS_MOVE_FAMILY_CAP:
        move_reasons.append(f"price-move rules capped at +{PTS_MOVE_FAMILY_CAP}")
        move_points = PTS_MOVE_FAMILY_CAP
    company += move_points
    ev.company_reasons.extend(move_reasons)

    if ANALYST_ACTION_RE.search(text):
        company += _add(ev.company_reasons, "analyst action", PTS_ANALYST_ACTION)
    if LEGAL_RE.search(text):
        company += _add(ev.company_reasons, "legal / regulatory / security", PTS_SECONDARY)
    if CAPITAL_RE.search(text):
        company += _add(ev.company_reasons, "capital or index action", PTS_SECONDARY)
    if RESTRUCTURING_RE.search(text):
        company += _add(ev.company_reasons, "restructuring", PTS_SECONDARY)
    if company > COMPANY_BONUS_CAP and not ev.fda_decision:
        ev.company_reasons.append(f"company events capped at +{COMPANY_BONUS_CAP}")
        company = COMPANY_BONUS_CAP
    ev.company_points = company

    # ── filler ──────────────────────────────────────────────────────────────
    penalty = 0
    for label, rx in FILLER_RULES:
        if rx.search(text):
            step = PENALTY_FILLER_FIRST if penalty == 0 else PENALTY_FILLER_EXTRA
            step = min(step, PENALTY_FILLER_CAP - penalty)
            if step <= 0:
                break
            penalty += step
            ev.filler_reasons.append(f"{label} (-{step})")
    ev.filler_penalty = penalty
    return ev
