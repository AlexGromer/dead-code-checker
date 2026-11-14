# How precision is measured

A dead-code checker is only useful if what it reports is dead. This page says how the precision of dead-code-checker is
measured, so that a number quoted for it can be checked, repeated and argued with. It describes the procedure; the
results of a run are published next to the date, the version of the checker and the sample they were measured on.

## What is measured

**Precision** is the share of reported findings that are correct:

    precision = true findings / (true findings + false findings)

over a set of findings of one kind (for example `dc.dead` in one language). It answers "when the checker says this is
dead, how often is it right?", which is what a reviewer decides on.

**Recall**, the share of all dead code that is found, is a different question and is not part of this procedure: the
total amount of dead code in a repository is not known, and a labelled count of it would be a guess. The checker is built
to prefer precision over recall (see the README), and recall is only discussed where a seeded corpus makes it checkable.

## What counts as a true finding

Fixed before any labelling and written in the instructions the labellers get:

- **True**: the finding names code that nothing in the project reaches, and removing it (or only it, for a symbol) keeps
  the project building and its tests passing, or the labeller can show from the code that no entry point, registration,
  reflection target, export or documented public interface reaches it.
- **False**: something does use it (a caller the checker missed, a framework convention, a dynamic lookup, a public API
  of a library, a build or deployment reference), or the finding does not describe the code it points at.
- **Unclear**: the labeller cannot decide from the repository. Unclear findings are counted separately, reported as a
  number, and excluded from the ratio; a result with many of them says so.

A finding about code that the project owners keep on purpose (a documented extension point) is false for a checker that
was told the project is a closed application, and true or not applicable for one told it is a library; the configured
mode of the run is part of what is recorded.

## The sample

1. **Disjoint from development.** The repositories (and the commits) used to design the rules, tune a threshold or fix a
   bug are never used to measure. The measurement set is chosen from repositories that were not opened during
   development, and the list of them is written down before the checker runs on them.
2. **Fixed before labelling.** The checker is run once on the measurement set with a released version. The findings are
   frozen as a file with its hash. Nothing is added, dropped or re-run after labelling starts.
3. **Drawn at random with a recorded seed**, stratified by language and by finding code, so a common finding in a large
   repository does not hide a rare one. Each stratum has a target size; a stratum with fewer findings is taken whole and
   is reported with its real count.
4. **One finding, one unit.** Several findings in the same file are drawn independently, and the interval below treats
   the units as independent. Where that is clearly wrong (one dead module producing many findings), the unit is the
   module, not the finding, and the report says so.

## Blind labelling

- The labeller gets the code, the path, the line and the kind of claim ("this function is never called"), and access to
  the whole repository. They do not get the checker's confidence, level, explanation or the name of the rule that fired,
  and the order is shuffled, so a label is not influenced by what the tool believes.
- Each finding is labelled **independently by two people**. Agreement is reported (Cohen's kappa and the raw share). A
  disagreement goes to a third labeller who sees both labels and the reasons, and decides.
- Labellers write one line of reason for every false or unclear label. These lines are the most useful output: they
  become the list of causes of false findings.
- Labelling is done on the repository at the commit the checker saw, not on a later version.

## The interval

A single ratio hides how few cases it rests on. Every reported precision comes with a **95 % Wilson score interval**,
which behaves well for small samples and for ratios near 0 or 1, where the plain normal interval does not.

For k true findings out of n labelled (unclear excluded), p = k / n and z = 1.96:

    centre = (p + z^2 / 2n) / (1 + z^2 / n)
    half   = z * sqrt(p (1 - p) / n + z^2 / 4n^2) / (1 + z^2 / n)
    interval = [centre - half, centre + half]      (clamped to 0 and 1)

```python
from math import sqrt

def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)

wilson(45, 50)   # (0.786, 0.957): 90 % observed, but 50 cases cannot rule out 79 %
wilson(20, 20)   # (0.839, 1.0):   no false finding in 20 does not mean precision 100 %
```

The lower end of the interval is the number to quote when a claim has to hold ("at least"). A stratum is reported only
with its n and k; a stratum with fewer than 20 labelled findings is shown but not summarised.

## What is published with a result

- the version of the checker and its configuration (mode, enabled languages, engines that were present);
- the list of repositories, their commit ids and licences, and the seed;
- n, k, the number of unclear findings and the interval, for every stratum and for the whole;
- the agreement of the labellers;
- the causes of the false findings, grouped, with one example for each;
- the labelled file, so anyone can recompute every number.

## Rules that keep it honest

- **A sample is used once.** When the checker is changed to fix false findings found on a sample, that sample no longer
  measures the new version; a fresh disjoint sample does. The old numbers stay published as the numbers of the old
  version.
- **No relabelling to fit.** A label is changed only by a documented correction of a labelling mistake, never because
  the result is unwelcome.
- **Comparisons use the same sample, the same units and the same definition of true** for every tool, and each tool is run
  with its documented default configuration plus, separately, the configuration its authors recommend for the project
  type. A tool that reports a different kind of finding is compared on the kinds that overlap only.
- **Negative results are published.** A stratum with poor precision is listed, with its causes.

## Limits of this procedure

Two labellers can agree and still be wrong; dead code in a dynamic language can look alive to every reader. The
repositories are a sample of open source code and say little about a private codebase with different conventions. The
interval covers the sampling of findings, not these two effects. They are the reason the causes of false findings are
published and not only the ratio.
