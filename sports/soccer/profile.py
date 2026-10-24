"""Soccer's own rules, on top of the data in config/sports.yaml."""

from dataclasses import dataclass

from sports.core.profile import SportProfile


@dataclass
class SoccerProfile(SportProfile):
    def classify(self, said, signals):
        """Soccer's combinations: a penalty that goes in is a penalty goal, one
        that's saved or missed a missed penalty, and an own goal is never
        reported as a plain goal."""
        kinds = {k for k, _ in said}
        if "own_goal" in kinds:
            said = [(k, w) for k, w in said if k != "goal"]
        if "penalty" in kinds and "goal" in kinds:
            said = [("penalty_goal", w) for k, w in said if k in ("penalty", "goal")] + \
                   [(k, w) for k, w in said if k not in ("penalty", "goal")]
        elif "penalty" in kinds and "save" in kinds:
            said = [("penalty_miss", w) for k, w in said if k in ("penalty", "save")] + \
                   [(k, w) for k, w in said if k not in ("penalty", "save")]
        return super().classify(said, signals)
