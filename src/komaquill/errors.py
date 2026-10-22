"""利用者へ伝える操作エラー。"""


class KomaquillError(Exception):
    """操作を継続できないときのエラー。"""


class SubmissionUnknown(KomaquillError):
    """送信後の応答が得られず受付結果が不明な状態。"""


class AllocationRefused(KomaquillError):
    """Colabが新規確保を明示的に拒否した。"""
