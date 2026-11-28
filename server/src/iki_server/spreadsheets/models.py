"""Importing a spreadsheet into records (ADR 0178): the import, its preview's rows, and
the mappings a project saves for the next sheet of the same kind.

An import is its requester's while they set it up and preview it; once it's committed, it
belongs to its project as the run that writes it does.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.org.models import Project


class ImportState(models.TextChoices):
    #: Iki is reading the workbook: its sheets and their first rows.
    READING = "reading", "Reading the workbook"
    #: Its sheet, rows and columns are being chosen.
    MAPPING = "mapping", "Mapping its columns"
    #: Every row is being read and checked, as the import would write it.
    PREVIEWING = "previewing", "Checking its rows"
    PREVIEWED = "previewed", "Checked"
    #: Committed: Iki is getting its rows ready for the run that writes them.
    COMMITTING = "committing", "Starting the import"
    #: Its run writes it, or wrote it: the run says how it went.
    COMMITTED = "committed", "Imported by its run"
    #: The workbook couldn't be read, or its preview couldn't be made.
    FAILED = "failed", "Failed"
    DISCARDED = "discarded", "Discarded"


class PreviewPhase(models.TextChoices):
    #: Reading the sheet's rows as values, into the rows to check.
    READING = "reading", "Reading"
    #: Checking those a chunk at a time, as a create is checked.
    CHECKING = "checking", "Checking"
    #: Finding what each would be: a new record, a duplicate, the same as another row.
    RESOLVING = "resolving", "Finding duplicates"
    DONE = "done", "Done"


class SavedMapping(models.Model):
    """A project's mapping of a kind of sheet's columns to a type's fields: an
    instrument's export, or a vendor's catalogue, so the next one maps itself."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    type = models.CharField(max_length=112, db_comment="The entity type it maps to.")
    name = models.CharField(max_length=100)
    revision = models.PositiveIntegerField(default=1, db_default=1)
    #: Each column it maps, by its header: {"header": …, "field": …, and how it reads}.
    columns = models.JSONField()
    #: The headers it was saved from, as compared: a sheet with them is suggested it.
    headers = models.JSONField(default=list)
    header_row = models.PositiveIntegerField(default=1)
    #: How the sheets' text writes numbers and dates, as :class:`cells.Locale` keeps it.
    locale = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_spreadsheet_mapping"
        db_table_comment = "Projects' saved mappings of spreadsheets' columns to a type's fields."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["project", "type", "name"], name="iki_spreadsheet_mapping_name"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="iki_spreadsheet_mapping_named"),
            models.CheckConstraint(condition=Q(revision__gt=0), name="iki_spreadsheet_mapping_rev"),
        ]

    def __str__(self) -> str:
        return self.name


class SpreadsheetImport(models.Model):
    """A workbook on its way into a project's records: its sheet and rows chosen, its
    columns mapped, every row checked, then written by a run (ADR 0178)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    type = models.CharField(max_length=112, db_comment="The entity type its rows become.")
    #: The workbook, kept as it was added (ADR 0036): every record names it as its origin.
    file = models.ForeignKey("iki_files.File", on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The agent that started it for ``requested_by``, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(
        max_length=16, choices=ImportState.choices, default=ImportState.READING
    )
    #: Why it failed, for people.
    error = models.TextField(blank=True, default="")
    #: ``xlsx`` or ``csv``; for CSV, how it was read.
    kind = models.CharField(max_length=8, blank=True, default="")
    encoding = models.CharField(max_length=16, blank=True, default="")
    separator = models.CharField(max_length=1, blank=True, default="")
    #: Each sheet: its name, rows, columns, whether it's hidden, and its first rows.
    sheets = models.JSONField(default=list)

    # What's imported, as its requester chooses.
    #: One more with each change to what it imports, Iki's first reading of the workbook
    #: among them. A change, a preview, a commit and saving its mapping each say the
    #: revision they were made from (If-Match): one made from another is refused, so
    #: nobody previews, imports or saves a mapping they haven't seen.
    revision = models.PositiveIntegerField(default=1, db_default=1)
    sheet = models.CharField(max_length=100, blank=True, default="")
    #: The row its columns' headers are in, from 1; 0 when it has none.
    header_row = models.PositiveIntegerField(default=1)
    #: That row's text, a cell a column, read from the workbook at its full width when
    #: the row is chosen: a sheet's sample holds only its first rows and columns.
    headers = models.JSONField(default=list)
    #: The rows imported, from ``first_row`` to ``last_row`` (its last, if none).
    first_row = models.PositiveIntegerField(default=2)
    last_row = models.PositiveIntegerField(null=True, blank=True)
    #: Rows left out besides: [[from, to], …].
    skip = models.JSONField(default=list)
    #: How its text writes numbers and dates, as the person said (:class:`cells.Locale`):
    #: until they have, cells that read two ways are problems, not guesses.
    locale = models.JSONField(default=dict)
    #: Each column it maps, as :class:`cells.Column` keeps it.
    columns = models.JSONField(default=list)
    #: The name and identity of the saved mapping when this draft selected it.
    mapping_source = models.JSONField(null=True, blank=True)

    # Its preview: each round is one, from the start; changing what's imported starts afresh.
    round = models.PositiveIntegerField(default=0)
    phase = models.CharField(max_length=16, choices=PreviewPhase.choices, blank=True, default="")
    progress = models.FloatField(default=0.0)
    progress_message = models.CharField(max_length=200, blank=True, default="")
    #: Bytes and lines of its rows checked so far: where the next chunk starts.
    offset = models.BigIntegerField(default=0)
    lines = models.BigIntegerField(default=0)
    #: The size of its rows as read, to check.
    size = models.BigIntegerField(default=0)
    #: What its preview found: how many rows of each kind.
    counts = models.JSONField(default=dict)
    #: The installed plugins it was checked against (as a bulk import keeps them).
    schema = models.CharField(max_length=64, blank=True, default="")
    failures = models.PositiveSmallIntegerField(default=0)

    # Its commit.
    #: The exact mapping confirmed at commit, independent of the reusable template.
    applied_mapping = models.JSONField(null=True, blank=True)
    #: Leave out the rows with problems: the person said so, when they committed it.
    skip_problems = models.BooleanField(default=False)
    reason = models.TextField(blank=True, default="")
    #: How it was committed (web, api, mcp, cli), and the OAuth app it came through, for
    #: its run and the audit log: a task starts the run.
    via = models.CharField(max_length=16, blank=True, default="")
    client = models.CharField(max_length=255, blank=True, default="")
    #: The rows written, one create a line, as the run's creates file (ADR 0056).
    creates = models.ForeignKey(
        "iki_files.File", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    run = models.ForeignKey(
        "iki_runs.ActionRun", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    #: Its preview's rows are gone: a fortnight after it was committed, they're cleared.
    rows_cleared = models.BooleanField(default=False)

    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_spreadsheet_import"
        db_table_comment = "Spreadsheets on their way into records: mapped, previewed, then run."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_spreadsheet_import_project"),
            models.Index(
                fields=["updated_at"],
                name="iki_spreadsheet_import_open",
                condition=Q(state__in=["reading", "previewing", "committing"]),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(state__in=ImportState.values), name="iki_spreadsheet_import_state"
            ),
            models.CheckConstraint(
                condition=Q(state="committed", run__isnull=False) | ~Q(state="committed"),
                name="iki_spreadsheet_import_run",
            ),
            models.CheckConstraint(
                condition=Q(revision__gt=0), name="iki_spreadsheet_import_revision"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.file_id} into {self.project_id}"


class RowOutcome(models.TextChoices):
    #: A new record, of a type without an identity.
    CREATE = "create", "New record"
    #: A new record of a new registry entry (ADR 0115).
    REGISTER = "register", "New entry"
    #: A new record, here, of an entry registered already.
    ADD = "add", "New record of a registered entry"
    #: The project's record of its entry: kept as it is.
    REUSE = "reuse", "Already in the project"
    #: The same as an earlier row: one record of both.
    SAME = "same", "Same as an earlier row"
    #: Something about it is wrong: it can't be written as it is.
    PROBLEM = "problem", "Has problems"


class PreviewRow(models.Model):
    """A row of a preview: what writing it would do, and what's wrong with it. Kept until
    the import is committed or discarded, keyed by import, round and row, as a bulk
    import's staged lines are (ADR 0056)."""

    pk = models.CompositePrimaryKey("spreadsheet_import_id", "round", "row")
    #: Not a foreign key: a big sheet's rows needn't each check it, and they go with it.
    spreadsheet_import_id = models.UUIDField()
    round = models.PositiveIntegerField()
    #: Its row in the sheet, from 1.
    row = models.BigIntegerField()
    #: Its line in the rows checked (and in the run's creates file), from 1.
    line = models.BigIntegerField()
    outcome = models.CharField(max_length=16, choices=RowOutcome.choices)
    identity_hash = models.CharField(max_length=64, null=True)
    #: Its unique fields' values: {key: value}, to find two rows with one.
    uniques = models.JSONField(null=True)
    #: The record or entry it is, if it's one already: its ID and HRID.
    match_id = models.UUIDField(null=True)
    match_hrid = models.CharField(max_length=32, blank=True, default="")
    #: The earlier row it's the same as.
    same_as = models.BigIntegerField(null=True)
    #: What's wrong: [{column, field, detail}], ``column`` from 0, or null for the row.
    problems = models.JSONField(null=True)
    #: Rules it breaks that hold it for review once it's imported (ADR 0127).
    held = models.JSONField(null=True)

    class Meta:
        db_table = "iki_spreadsheet_row"
        db_table_comment = "Spreadsheet imports' previews: what each row would do, until written."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["spreadsheet_import_id", "round", "identity_hash"],
                name="iki_spreadsheet_row_identity",
                condition=Q(identity_hash__isnull=False),
            ),
            models.Index(
                fields=["spreadsheet_import_id", "round", "line"],
                name="iki_spreadsheet_row_line",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.spreadsheet_import_id} row {self.row}"
