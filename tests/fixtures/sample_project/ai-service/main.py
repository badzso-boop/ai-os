class Recommender:
    """Tiny placeholder recommender."""

    def score(self, item):
        return helper_score(item)


def helper_score(item):
    return len(item)
