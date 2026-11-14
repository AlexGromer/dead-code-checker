# Announcement drafts (not posted)

Rules for every text below: claims come from the README and docs of this repository; every number comes from the
published methodology run ([../methodology.md](../methodology.md)) and stays `{{n}}` until that run exists; limits are
stated in the first screen, not the last; no comparison claim about another tool that has not been checked against that
tool's own documentation on the day. Post when PyPI and the tagged release exist, on a weekday, and be present to answer
for the first hours.

---

## Show HN

**Title:** Show HN: dead-code-checker, dead code in what a commit adds (no model, no network)

**Text:**

> I kept finding code in reviews that nothing called: a helper added "for later", a hook that ignores its input and
> prints a constant, a config key nobody reads. So I wrote a checker that looks only at what a change adds and asks one
> question: can anything reach this?
>
> It is deterministic and uses only the Python standard library. No model, no network. There is a pre-commit hook, a
> GitHub Action (SARIF to code scanning, one PR comment, fails only on errors) and a command that audits a tree that is
> not yours without running anything in it.
>
> What it does not do, up front: names are matched as text in the original checker, so a dynamic use (`getattr` with a
> built string, a plugin loader) is invisible and needs an ignore marker; in the extra languages only top-level
> declarations are read. It prefers precision over recall: when in doubt the symbol counts as used. A newer graph checker
> (`dead-code-checker graph`) builds a reachability graph instead of matching text; it is a preview.
>
> How I measure precision, so you can argue with the number: blind labelling by two people on a sample disjoint from
> the code the rules were written against, with a Wilson interval. The procedure is at docs/methodology.md. Result on
> {{n}} findings in {{repos}} repositories: {{precision}} ({{low}} to {{high}}); the causes of the false ones are listed.
>
> Repo: https://github.com/AlexGromer/dead-code-checker. I would most like to hear about false findings in your language.

**First comment (post it yourself right after):** the three most common causes of false findings from the run, each with
one real example, and what is planned for each.

---

## Habr (draft in Russian)

**Заголовок:** Мёртвый код в том, что добавил коммит: детерминированный чекер без моделей и сети

**Текст:**

> Вопрос на ревью, который повторяется: «а это кто-нибудь вызывает?». Функция «на потом», хук, который игнорирует вход и
> печатает константу, ключ конфига, который никто не читает. Я сделал чекер, который смотрит только на добавленные
> строки и отвечает на один вопрос: достижимо ли это откуда-нибудь.
>
> **Что это.** Утилита на стандартной библиотеке Python 3.11+, без модели и без сети. Pre-commit хук, GitHub Action
> (SARIF в code scanning, один комментарий в PR, падает только на ошибках уровня E), режим аудита чужого дерева, в
> котором ничего не запускается и ничего не пишется.
>
> **Как устроено.** Первый чекер ищет имена текстом (`git grep -w`) и разбирает Python через `ast`. Второй, в стадии
> предварительной версии, строит граф достижимости символов от точек входа (манифесты, юнит-файлы, регистрации хуков) и
> различает «мёртв» и «не найден вызов, но это может быть рефлексия».
>
> **Ограничения.** Динамические обращения (`getattr` с собранной строкой, загрузчик плагинов) невидимы, нужен маркер
> `# deadcode: ignore`. В дополнительных языках читаются только объявления верхнего уровня. Приоритет за точностью, не за
> полнотой: при сомнении символ считается используемым.
>
> **Как измеряем точность.** Слепая разметка двумя людьми, выборка не пересекается с кодом, на котором писались правила,
> доверительный интервал Уилсона. Процедура описана в docs/methodology.md. Результат: {{n}} находок в {{repos}}
> репозиториях, точность {{precision}} ({{low}}–{{high}}); причины ложных находок разобраны в статье.
>
> Ссылка на репозиторий, установка в одну строку, примеры конфигурации. Буду рад ложным срабатываниям на вашем языке.

Хабы: Python, Open source, Программирование, Тестирование IT-систем (проверить актуальные названия). Тег про качество кода.

---

## Reddit

One post per subreddit, and only where self-promotion is allowed by the sidebar rules (read them on the day; some require
a weekly thread). Candidates: a Python subreddit, a programming-tools subreddit, a DevOps subreddit for the Action.

**Title:** I wrote a dead-code checker that only looks at what a commit adds (stdlib only, no model, no network)

**Body:**

> Link: https://github.com/AlexGromer/dead-code-checker
>
> It flags new functions nothing calls, new files nothing imports, stub bodies, unread config keys and unused CLI flags,
> in Python, shell, JavaScript, Go and Rust by default and more languages with a flag. pre-commit hook, GitHub Action,
> VS Code extension.
>
> Known weakness: dynamic use is invisible to the text checker; it errs toward "used". The precision procedure and
> its limits are written down at docs/methodology.md, and the numbers from a blind-labelled sample are {{precision}} on
> {{n}} findings.
>
> What would make it useful to you, and what makes it wrong on your code?

---

## dev.to

**Title:** Catching dead code in the pull request, not in the cleanup sprint

**Outline:**

1. The problem: dead code is cheapest to remove the day it is written; later nobody knows if it is safe.
2. The idea: look only at what the change adds; ask whether anything reaches it.
3. A worked example in three snippets: a function nobody calls, a hook that prints a constant, a config key nobody reads;
   the findings the checker prints for each (take the real output).
4. The GitHub Action in ten lines of YAML; the PR comment; SARIF in code scanning; why it fails only on E.
5. Honest limits: dynamic use, the ignore marker, precision over recall, how the precision is measured.
6. Try it: `pipx install dead-code-checker`, and where to report a false finding.

Cover image: a screenshot of the PR comment. Tags: python, devops, codequality, opensource.
