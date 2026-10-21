"""Encode recoverable application failures as MCP tool errors."""

from fastmcp.exceptions import ToolError

from helpdesk.errors import PublicError


class ToolFailure(ToolError):
    """Preserve a machine-readable error inside an MCP error content block.

    Parameters
    ----------
    error : PublicError
        Minimal public error and internal correlation metadata.
    """

    def __init__(self, error: PublicError):
        self.error = error
        self.code = error.code
        super().__init__(str(error))
