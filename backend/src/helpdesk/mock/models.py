"""Define request and response schemas for the mock ticket system."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
TicketId = Annotated[str, StringConstraints(
    strip_whitespace=True, to_upper=True, max_length=100, pattern=r"^[Tt][Kk][Tt]-[A-Za-z0-9]+$"
)]
ApprovalId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Comment(BaseModel):
    """Represent a comment embedded in a ticket."""

    id: str
    author_id: str
    body: str
    created_at: datetime


class Ticket(BaseModel):
    """Represent a ticket returned by the mock API."""

    id: str
    requester_id: str
    title: str
    description: str
    status: Literal["open", "in_progress", "resolved"]
    visibility: Literal["internal", "restricted"]
    created_at: datetime
    comments: list[Comment]


class CreateTicket(BaseModel):
    """Describe a ticket proposal or an attempt to execute its approved payload."""

    model_config = ConfigDict(extra="forbid")
    title: Title
    description: Text
    approval_id: ApprovalId | None = None


class AddComment(BaseModel):
    """Describe a comment proposal or an attempt to execute its approved payload."""

    model_config = ConfigDict(extra="forbid")
    body: Text
    approval_id: ApprovalId | None = None


class ApprovalDecision(BaseModel):
    """Represent an explicit decision submitted outside the model tool surface."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "decline"]


class TicketSummary(BaseModel):
    """Represent an accessible search candidate without its description."""

    id: str
    title: str
    status: Literal["open", "in_progress", "resolved"]
    visibility: Literal["internal", "restricted"]


class SearchResult(BaseModel):
    """Validate backend pagination before exposing it to an agent."""

    items: list[TicketSummary]
    page: Annotated[int, Field(ge=1, le=1000000)]
    page_size: Annotated[int, Field(ge=1, le=50)]
    total: Annotated[int, Field(ge=0)]
    has_more: bool


class TicketOverview(BaseModel):
    """Expose only the fields needed to choose a ticket candidate."""

    id: str
    title: str
    status: Literal["open", "in_progress", "resolved"]


class TicketListResult(BaseModel):
    """Describe a permission-filtered page of ticket overviews."""

    items: list[TicketOverview]
    page: Annotated[int, Field(ge=1, le=1000000)]
    page_size: Annotated[int, Field(ge=1, le=50)]
    total: Annotated[int, Field(ge=0)]
    has_more: bool


class AccessResult(BaseModel):
    """Describe a verified upstream user with current Helpdesk membership."""

    user_id: str
    role: Literal["employee", "support"]


class WriteProposal(BaseModel):
    """Represent a persisted action that has not yet changed ticket data."""

    approval_id: ApprovalId
    status: Literal["pending", "approved"]
    operation: Literal["create_ticket", "add_comment"]
    arguments: dict


class ApprovalState(WriteProposal):
    """Restore an application-owned approval, including terminal states."""

    status: Literal["pending", "approved", "declined", "completed"]


class CreatedTicketResult(BaseModel):
    """Represent the confirmed result of one approved ticket creation."""

    status: Literal["completed"]
    approval_id: ApprovalId
    ticket: Ticket


class AddedCommentResult(BaseModel):
    """Represent the confirmed result of one approved comment addition."""

    status: Literal["completed"]
    approval_id: ApprovalId
    ticket_id: str
    comment: Comment
