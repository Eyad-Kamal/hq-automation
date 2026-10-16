"""Notion database titles, property names and option values.

Single source of truth: ``build/schema-spec.md``. Property names are exact and
case-sensitive. Each database has a ``P_<Name>`` class; the title property is
always ``TITLE``. Relation and formula properties are included.

Synced (dual) relation properties that live on a *target* database are listed
on the target's class with a ``# synced`` comment.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Database titles (exact)
# ---------------------------------------------------------------------------
DB_LEVELS = "Levels"
DB_TOPICS = "Topics"
DB_MEMBERS = "Members"
DB_TEAMS = "Teams"
DB_TASKS = "Tasks"
DB_PROBLEMS = "Problems"
DB_CONTESTS = "Contests"
DB_CONTEST_ENTRIES = "Contest Entries"
DB_LEARNING_QUEUE = "Learning Queue & Mastery"
DB_GOALS = "Goals"
DB_RESOURCES = "Resources"
DB_CODE_TEMPLATES = "Code Templates"
DB_ACHIEVEMENTS = "Achievements"
DB_WEEKLY_REPORTS = "Weekly Reports"
DB_DAILY_LOG = "Daily Log"
DB_WEEKLY_SUMMARY = "Weekly Summary"
DB_RATING_HISTORY = "Rating History"
DB_CONFIG = "Config"
DB_SYNC_LOG = "Sync Log"

DATABASE_TITLES: tuple[str, ...] = (
    DB_LEVELS,
    DB_TOPICS,
    DB_MEMBERS,
    DB_TEAMS,
    DB_TASKS,
    DB_PROBLEMS,
    DB_CONTESTS,
    DB_CONTEST_ENTRIES,
    DB_LEARNING_QUEUE,
    DB_GOALS,
    DB_RESOURCES,
    DB_CODE_TEMPLATES,
    DB_ACHIEVEMENTS,
    DB_WEEKLY_REPORTS,
    DB_DAILY_LOG,
    DB_WEEKLY_SUMMARY,
    DB_RATING_HISTORY,
    DB_CONFIG,
    DB_SYNC_LOG,
)


# ---------------------------------------------------------------------------
# Property names per database
# ---------------------------------------------------------------------------
class P_Levels:
    """Database "Levels"."""

    TITLE = "Name"
    LEVEL_NO = "Level #"
    COLOR = "Color"
    RATING_GATE = "Rating Gate"
    BAND_LOW = "Band Low"
    BAND_HIGH = "Band High"
    DESCRIPTION = "Description"
    # synced
    TOPICS = "Topics"  # from Topics."Level"
    MEMBERS = "Members"  # from Members."Level"
    GOALS = "Goals"  # from Goals."Linked Level"
    RESOURCES = "Resources"  # from Resources."Levels"


class P_Topics:
    """Database "Topics"."""

    TITLE = "Name"
    CATEGORY = "Category"
    ORDER = "Order"
    SESSIONS = "Sessions"
    BAND_LOW = "Band Low"
    BAND_HIGH = "Band High"
    EST_HOURS = "Est Hours"
    CORE = "Core"
    CF_TAGS = "CF Tags"
    SUBTOPICS = "Subtopics"
    DESCRIPTION = "Description"
    # relations
    LEVEL = "Level"
    PREREQUISITES = "Prerequisites"
    # synced
    UNLOCKS = "Unlocks"  # from Topics."Prerequisites" (self)
    PROBLEMS = "Problems"  # from Problems."Topics"
    TASKS = "Tasks"  # from Tasks."Topic"
    LEARNERS = "Learners"  # from Learning Queue & Mastery."Topic"
    GOALS = "Goals"  # from Goals."Linked Topics"
    RESOURCES = "Resources"  # from Resources."Topics"
    CODE_TEMPLATES = "Code Templates"  # from Code Templates."Topics"
    # formulas
    BAND = "Band"


class P_Members:
    """Database "Members"."""

    TITLE = "Name"
    PERSON = "Person"
    ROLE = "Role"
    ACTIVE = "Active"
    CF_HANDLE = "CF Handle"
    ATCODER_HANDLE = "AtCoder Handle"
    LEETCODE_HANDLE = "LeetCode Handle"
    VJUDGE_HANDLE = "Vjudge Handle"
    OTHER_HANDLES = "Other Handles"
    EMAIL = "Email"
    CONTEST_SUBSCRIPTIONS = "Contest Subscriptions"
    CF_RATING = "CF Rating"
    CF_MAX_RATING = "CF Max Rating"
    ATCODER_RATING = "AtCoder Rating"
    POINTS_TODAY = "Points Today"
    POINTS_THIS_WEEK = "Points This Week"
    POINTS_LAST_WEEK = "Points Last Week"
    TOTAL_POINTS = "Total Points"
    PROBLEMS_TOTAL = "Problems Total"
    CURRENT_STREAK = "Current Streak"
    BEST_STREAK = "Best Streak"
    FREEZES = "Freezes"
    LAST_ACTIVE = "Last Active"
    DAILY_TARGET = "Daily Target"
    WEEKLY_TARGET_POINTS = "Weekly Target Points"
    DASHBOARD = "Dashboard"
    JOINED = "Joined"
    LAST_SYNC = "Last Sync"
    SEASON_START = "Season Start"  # coach reset: this member's season starts here (date)
    SEASON_POINTS = "Season Points"  # points of deduped solves in season
    SEASON_SOLVED = "Season Solved"  # deduped solves in season
    # relations
    TEAM = "Team"
    LEVEL = "Level"
    # synced
    PROBLEMS = "Problems"  # from Problems."Member"
    TASKS = "Tasks"  # from Tasks."Assignee"
    LEARNING_QUEUE = "Learning Queue"  # from Learning Queue & Mastery."Member"
    GOALS = "Goals"  # from Goals."Member"
    RESOURCES_ADDED = "Resources Added"  # from Resources."Added By Member"
    CODE_TEMPLATES = "Code Templates"  # from Code Templates."Author"
    ACHIEVEMENTS = "Achievements"  # from Achievements."Member"
    DAILY_LOGS = "Daily Logs"  # from Daily Log."Member"
    WEEKLY_SUMMARIES = "Weekly Summaries"  # from Weekly Summary."Member"
    RATING_HISTORY = "Rating History"  # from Rating History."Member"
    CONTEST_ENTRIES = "Contest Entries"  # from Contest Entries."Member"
    # formulas
    REF_RATING = "Ref Rating"
    RANK = "Rank"
    WEEK_DELTA = "Week Δ"
    TREND = "Trend"
    STREAK_BADGE = "Streak Badge"


class P_Teams:
    """Database "Teams"."""

    TITLE = "Name"
    TEAM_TYPE = "Team Type"
    COACH = "Coach"
    POINTS_THIS_WEEK = "Points This Week"
    POINTS_LAST_WEEK = "Points Last Week"
    TOTAL_POINTS = "Total Points"
    NOTES = "Notes"
    # synced
    MEMBERS = "Members"  # from Members."Team"
    CONTEST_ENTRIES = "Contest Entries"  # from Contest Entries."Team"
    GOALS = "Goals"  # from Goals."Team"
    WEEKLY_SUMMARIES = "Weekly Summaries"  # from Weekly Summary."Team"
    # formulas
    SIZE = "Size"
    AVG_CF_RATING = "Avg CF Rating"


class P_Problems:
    """Database "Problems"."""

    TITLE = "Problem"
    WHO = "Who"
    TEAM = "Team"
    JUDGE = "Judge"
    PROBLEM_KEY = "Problem Key"
    LINK = "Link"
    RATING = "Rating"
    DIFFICULTY = "Difficulty"
    CF_TAGS = "CF Tags"
    SOLVED_ON = "Solved On"
    MODE = "Mode"
    STATUS = "Status"
    TIME_MIN = "Time (min)"
    ATTEMPTS = "Attempts"
    RATING_AT_SOLVE = "Rating @ Solve"
    IMPORTED = "Imported"
    SOURCE_KEY = "Source Key"
    IN_SEASON = "In Season"  # checkbox: solve is on or after the member's effective season start
    KEY_IDEA = "Key Idea"
    NOTES = "Notes"
    CREATED = "Created"
    # relations
    MEMBER = "Member"
    TOPICS = "Topics"
    AUTO_TOPICS = "Auto Topics"  # rich text: comma-separated Topic ids the script last wrote to Topics (see recompute)
    # synced
    CONTEST_ENTRY = "Contest Entry"  # from Contest Entries."Problems"
    # formulas
    EFFECTIVE_RATING = "Effective Rating"
    POINTS = "Points"
    RATING_BUCKET = "Rating Bucket"


class P_Contests:
    """Database "Contests"."""

    TITLE = "Name"
    PLATFORM = "Platform"
    KIND = "Kind"
    START = "Start"
    DURATION_MIN = "Duration (min)"
    LINK = "Link"
    PHASE = "Phase"
    RATED = "Rated"
    EXTERNAL_ID = "External ID"
    TASKS_CREATED = "Tasks Created"
    ALERT_SENT = "Alert Sent"
    # synced
    ENTRIES = "Entries"  # from Contest Entries."Contest"
    TASKS = "Tasks"  # from Tasks."Contest"
    # formulas
    STARTS_IN = "Starts In"


class P_ContestEntries:
    """Database "Contest Entries"."""

    TITLE = "Entry"
    MODE = "Mode"
    WHO = "Who"
    DATE = "Date"
    PARTICIPATION = "Participation"
    STATUS = "Status"
    SOLVED = "Solved"
    TOTAL_PROBLEMS = "Total Problems"
    UPSOLVED = "Upsolved"
    RANK = "Rank"
    RATING_DELTA = "Rating Δ"
    PERFORMANCE = "Performance"
    PENALTY = "Penalty"
    BLOCKERS = "Blockers"
    TAKEAWAY = "Takeaway"
    IN_SEASON = "In Season"  # checkbox
    # relations
    CONTEST = "Contest"
    MEMBER = "Member"
    TEAM = "Team"
    PROBLEMS = "Problems"
    # synced
    TASKS = "Tasks"  # from Tasks."Contest Entry"
    # formulas
    UPSOLVE_PCT = "Upsolve %"
    CONTEST_KIND = "Contest Kind"


class P_Tasks:
    """Database "Tasks"."""

    TITLE = "Task"
    ASSIGNEE_PERSON = "Assignee Person"
    WHO = "Who"
    TYPE = "Type"
    STATUS = "Status"
    DUE = "Due"
    ORIGIN = "Origin"
    PRIORITY = "Priority"
    TARGET = "Target"
    DONE_COUNT = "Done Count"
    LINKS = "Links"
    NOTES = "Notes"
    SOURCE_KEY = "Source Key"
    CREATED = "Created"
    # relations
    ASSIGNEE = "Assignee"
    TOPIC = "Topic"
    CONTEST = "Contest"
    CONTEST_ENTRY = "Contest Entry"
    # formulas
    PROGRESS = "Progress"
    OVERDUE = "Overdue"


class P_LearningQueue:
    """Database "Learning Queue & Mastery"."""

    TITLE = "Name"
    WHO = "Who"
    QUEUE_STATUS = "Queue Status"
    PRIORITY = "Priority"
    DEADLINE = "Deadline"
    STARTED = "Started"
    HOURS_SPENT = "Hours Spent"
    THEORY_DONE = "Theory Done"
    PROBLEMS_COUNT = "Problems Count"
    BELOW_BAND = "Below Band"
    IN_BAND = "In Band"
    ABOVE_BAND = "Above Band"
    MAX_RATING = "Max Rating"
    MASTERY_POINTS = "Mastery Points"
    AUTO_TIER = "Auto Tier"
    COACH_OVERRIDE = "Coach Override"
    NOTES = "Notes"
    # relations
    MEMBER = "Member"
    TOPIC = "Topic"
    # formulas
    TIER = "Tier"
    LEVEL = "Level"
    BAND = "Band"
    EST_HOURS = "Est Hours"
    DEADLINE_STATUS = "Deadline Status"


class P_Goals:
    """Database "Goals"."""

    TITLE = "Goal"
    WHO = "Who"
    SCOPE = "Scope"
    METRIC = "Metric"
    THRESHOLD_RATING = "Threshold Rating"
    START_VALUE = "Start Value"
    CURRENT = "Current"
    TARGET = "Target"
    START = "Start"
    DEADLINE = "Deadline"
    STATUS = "Status"
    WHY = "Why"
    # relations
    MEMBER = "Member"
    TEAM = "Team"
    LINKED_LEVEL = "Linked Level"
    LINKED_TOPICS = "Linked Topics"
    # formulas
    PROGRESS = "Progress"
    PROGRESS_BAR = "Progress Bar"
    PACE = "Pace"


class P_Resources:
    """Database "Resources"."""

    TITLE = "Name"
    LINK = "Link"
    TYPE = "Type"
    ADDED_BY = "Added By"
    WHAT_IT_SERVES = "What It Serves"
    LANGUAGE = "Language"
    QUALITY = "Quality"
    FREE = "Free"
    RECOMMENDED = "Recommended"
    CREATED = "Created"
    # relations
    TOPICS = "Topics"
    LEVELS = "Levels"
    ADDED_BY_MEMBER = "Added By Member"


class P_CodeTemplates:
    """Database "Code Templates"."""

    TITLE = "Name"
    CATEGORY = "Category"
    LANGUAGE = "Language"
    AUTHOR_PERSON = "Author Person"
    COMPLEXITY = "Complexity"
    TESTED = "Tested"
    VERIFIED_ON = "Verified On"
    USAGE_NOTES = "Usage Notes"
    CREATED = "Created"
    # relations
    TOPICS = "Topics"
    AUTHOR = "Author"


class P_Achievements:
    """Database "Achievements"."""

    TITLE = "Name"
    WHO = "Who"
    BADGE = "Badge"
    TIER = "Tier"
    EARNED_ON = "Earned On"
    DETAIL = "Detail"
    SOURCE_KEY = "Source Key"
    # relations
    MEMBER = "Member"


class P_WeeklyReports:
    """Database "Weekly Reports"."""

    TITLE = "Name"
    WEEK_START = "Week Start"
    WEEK_LABEL = "Week Label"
    CLUB_POINTS = "Club Points"
    CLUB_PROBLEMS = "Club Problems"
    MVP = "MVP"
    HIGHLIGHTS = "Highlights"
    # synced
    SUMMARIES = "Summaries"  # from Weekly Summary."Report"


class P_DailyLog:
    """Database "Daily Log"."""

    TITLE = "Name"
    WHO = "Who"
    TEAM = "Team"
    DATE = "Date"
    WEEK = "Week"
    PROBLEMS = "Problems"
    POINTS = "Points"
    CUMULATIVE_POINTS = "Cumulative Points"  # added in round 3: member's running total up to and including this day
    CONTESTS = "Contests"
    MAX_RATING_SOLVED = "Max Rating Solved"
    ACTIVE = "Active"
    FROZEN = "Frozen"
    STREAK = "Streak"
    TARGET = "Target"
    MINUTES = "Minutes"
    REFLECTION = "Reflection"
    SOURCE_KEY = "Source Key"
    IN_SEASON = "In Season"  # checkbox
    # relations
    MEMBER = "Member"
    # formulas
    TARGET_MET = "Target Met"


class P_WeeklySummary:
    """Database "Weekly Summary"."""

    TITLE = "Name"
    SCOPE = "Scope"
    WHO = "Who"
    WEEK_START = "Week Start"
    WEEK_LABEL = "Week Label"
    POINTS = "Points"
    PROBLEMS = "Problems"
    ACTIVE_DAYS = "Active Days"
    AVG_RATING = "Avg Rating"
    MAX_RATING = "Max Rating"
    CONTESTS = "Contests"
    RATING_DELTA = "Rating Δ"
    POINTS_DELTA = "Points Δ"
    TOPICS_ADVANCED = "Topics Advanced"
    MVP = "MVP"
    SOURCE_KEY = "Source Key"
    IN_SEASON = "In Season"  # checkbox
    # relations
    MEMBER = "Member"
    TEAM = "Team"
    REPORT = "Report"
    # formulas
    TREND = "Trend"


class P_RatingHistory:
    """Database "Rating History"."""

    TITLE = "Name"
    WHO = "Who"
    PLATFORM = "Platform"
    DATE = "Date"
    OLD = "Old"
    NEW = "New"
    RANK = "Rank"
    CONTEST_NAME = "Contest Name"
    SOURCE_KEY = "Source Key"
    # relations
    MEMBER = "Member"
    # formulas
    DELTA = "Δ"


class P_Config:
    """Database "Config". The title property holds the config key."""

    TITLE = "Key"
    VALUE = "Value"
    NUMBER = "Number"
    DATE = "Date"  # date property; SEASON_START reads it first
    DESCRIPTION = "Description"


class P_SyncLog:
    """Database "Sync Log"."""

    TITLE = "Run"
    STARTED = "Started"
    JOB = "Job"
    RESULT = "Result"
    ITEMS = "Items"
    DETAILS = "Details"


# ---------------------------------------------------------------------------
# Option values used by the code (Notion select / status option names)
# ---------------------------------------------------------------------------
class Platform:
    """Values of Contests."Platform" and Rating History."Platform"."""

    CODEFORCES = "Codeforces"
    CF_GYM = "CF Gym"
    ATCODER = "AtCoder"
    LEETCODE = "LeetCode"


class Judge:
    """Values of Problems."Judge"."""

    CODEFORCES = "Codeforces"
    CF_GYM = "CF Gym"
    ATCODER = "AtCoder"
    LEETCODE = "LeetCode"


class Mode:
    """Values of Problems."Mode"."""

    LIVE = "Live Contest"
    VIRTUAL = "Virtual"
    PRACTICE = "Practice"
    UPSOLVE = "Upsolve"
    EDITORIAL = "With Editorial/Hint"


class Difficulty:
    """Values of Problems."Difficulty"."""

    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"


class GoalMetric:
    """Values of Goals."Metric". The last three use the goal's links: Topic metrics need "Linked Topics",
    Level Topics ≥ Competent needs "Linked Level" (see hq.logic.goals)."""

    PROBLEMS_SOLVED = "Problems Solved"
    POINTS = "Points"
    CF_RATING = "CF Rating"
    LEVEL_REACHED = "Level Reached"
    TOPICS_COMPETENT = "Topics ≥ Competent"
    STREAK_DAYS = "Streak Days"
    CONTESTS_ATTENDED = "Contests Attended"
    PROBLEMS_AT_RATING = "Problems ≥ Rating"
    TOPIC_PROBLEMS = "Topic Problems"
    TOPIC_TIER = "Topic Tier"
    LEVEL_TOPICS_COMPETENT = "Level Topics ≥ Competent"


class ConfigKey:
    """Keys of the Config database (Config."Key")."""

    TIMEZONE = "TIMEZONE"
    WEEK_START = "WEEK_START"
    GATE_L1 = "GATE_L1"
    GATE_L2 = "GATE_L2"
    GATE_L3 = "GATE_L3"
    GATE_L4 = "GATE_L4"
    TIER_NOVICE = "TIER_NOVICE"
    TIER_APPRENTICE = "TIER_APPRENTICE"
    TIER_COMPETENT = "TIER_COMPETENT"
    TIER_PROFICIENT = "TIER_PROFICIENT"
    TIER_PROFICIENT_ABOVE = "TIER_PROFICIENT_ABOVE"
    TIER_MASTER = "TIER_MASTER"
    TIER_MASTER_ABOVE = "TIER_MASTER_ABOVE"
    BAND_BELOW_FACTOR = "BAND_BELOW_FACTOR"
    BAND_IN_FACTOR = "BAND_IN_FACTOR"
    BAND_ABOVE_FACTOR = "BAND_ABOVE_FACTOR"
    MODE_UPSOLVE = "MODE_UPSOLVE"
    MODE_EDITORIAL = "MODE_EDITORIAL"
    ATCODER_OFFSET = "ATCODER_OFFSET"
    LC_EASY = "LC_EASY"
    LC_MEDIUM = "LC_MEDIUM"
    LC_HARD = "LC_HARD"
    AUTO_TASK_PROBLEMS = "AUTO_TASK_PROBLEMS"
    ALERT_MINUTES = "ALERT_MINUTES"
    CONTEST_TASK_LEAD_HOURS = "CONTEST_TASK_LEAD_HOURS"
    FREEZE_EVERY = "FREEZE_EVERY"
    FREEZE_MAX = "FREEZE_MAX"
    DEFAULT_DAILY_TARGET = "DEFAULT_DAILY_TARGET"
    ADMIN_USER_ID = "ADMIN_USER_ID"
    IMPORT_BACKFILL_DAYS = "IMPORT_BACKFILL_DAYS"  # added by B3: first-import window (days)
    IMPORT_MAX_CREATES = "IMPORT_MAX_CREATES"  # added by B3 (fix round 1): creates per import run
    UPSOLVE_WINDOW_DAYS = "UPSOLVE_WINDOW_DAYS"  # upsolve reminders only for contests that ended this many days ago
    DAILY_LOG_DAYS = "DAILY_LOG_DAYS"  # Daily Log rows recomputed per member (days back from today)
    WEEKLY_BACKFILL_WEEKS = "WEEKLY_BACKFILL_WEEKS"  # complete past weeks whose Weekly Summary rows are kept filled
    DASHBOARDS_AUTO = "DASHBOARDS_AUTO"  # 1 = create personal dashboard pages for members with an empty Dashboard URL
    AUTO_PRACTICE_TASKS = "AUTO_PRACTICE_TASKS"  # 1 = daily auto Topic Practice tasks; 0 (default) = coaches assign them
    LEETCODE_DAILY_TASKS = "LEETCODE_DAILY_TASKS"  # 1 = one "LeetCode daily" task per Active member each day
    ICON_BACKFILL_PER_RUN = "ICON_BACKFILL_PER_RUN"  # most empty row icons set per run (auto job)
    TITLE_FIX_PER_RUN = "TITLE_FIX_PER_RUN"  # most old emoji title prefixes removed per run (onboard job)
    SEASON_START = "SEASON_START"  # Config "Date" (or Value YYYY-MM-DD): global season start; empty = no season
    SEASON_FLAG_PER_RUN = "SEASON_FLAG_PER_RUN"  # most In Season checkboxes written per recompute run
    TOPIC_ROUTE_PER_RUN = "TOPIC_ROUTE_PER_RUN"  # most Problems rows whose Topics recompute rewrites per run


# Source Key prefix of the automatic "LeetCode daily" task: "lcdaily:<date>:<member id>".
LC_DAILY_SOURCE_PREFIX = "lcdaily:"

# CF tag vocabulary (options of "CF Tags" on Topics and Problems).
CF_TAGS: tuple[str, ...] = (
    "implementation",
    "math",
    "greedy",
    "dp",
    "data structures",
    "brute force",
    "constructive algorithms",
    "graphs",
    "sortings",
    "binary search",
    "dfs and similar",
    "trees",
    "strings",
    "number theory",
    "combinatorics",
    "geometry",
    "bitmasks",
    "two pointers",
    "dsu",
    "shortest paths",
    "probabilities",
    "divide and conquer",
    "hashing",
    "games",
    "interactive",
    "flows",
    "matrices",
    "string suffix structures",
    "graph matchings",
    "fft",
    "ternary search",
    "expression parsing",
    "meet-in-the-middle",
    "2-sat",
    "chinese remainder theorem",
    "schedules",
    "special",
)

# Badge options of Achievements."Badge".
BADGES: tuple[str, ...] = (
    "First Blood",
    "Century (100)",
    "250 Club",
    "500 Club",
    "1000 Club",
    "Week Warrior (7🔥)",
    "Iron Habit (30🔥)",
    "Unbreakable (100🔥)",
    "Stretch 1200",
    "Stretch 1400",
    "Stretch 1600",
    "Stretch 2000",
    "Topic Mastered",
    "Level Complete",
    "Pupil Reached",
    "Specialist Reached",
    "Expert Reached",
    "CM Reached",
    "Weekly MVP",
    "Contest Regular",
    "Upsolve Hero",
    "Template Smith",
    "Librarian",
    "Team Player",
)
