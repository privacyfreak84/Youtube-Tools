class SourceError(Exception):
    """Something went wrong talking to YouTube (reading a list, one download). The message is user-ready."""


class DownloadError(SourceError):
    """One download failed. Other downloads can still go ahead."""


class DownloadStopped(Exception):
    """Raised inside a running download when the user pressed Ctrl-C, so it ends quickly. Not an error."""
