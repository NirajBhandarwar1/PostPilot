"""BUILD.md bugs found in a real generated file: invalid PowerShell, typographic hyphens in commands,
pip scanner reading prose, ''' fences and escaped '#', model id mismatch, duplicate checks."""

import re

from postpilot import brain, factgate, render
from tests.test_options import IDEA, fake_brief
from tests.test_state_and_safety import _kit

FENCE = "`" * 3
NB = "‑"       # non-breaking hyphen: what the model wrote in "docx-edit"


def _doc(brief, idea=None, checks=(), plan_checks=(), post="post"):
    idea = idea or dict(IDEA, handle="a/b", handle_type="github_repo")
    return render.render_brief(idea, brief, _kit(), post, (70, 76, "PASS"), {}, list(checks), list(plan_checks))


def _part_a(doc):
    return doc[:doc.index("Cursor: stop here")]


def _task(details, title="Add code", done_when="it runs"):
    return brain.BuildTask(title=title, details=details, done_when=done_when)


# ---------- 1. Step 1 PowerShell ----------

def test_step1_requirements_line_is_valid_powershell_from_the_brief():
    doc = _doc(fake_brief(uses_models=True))
    assert "@('openai', 'pytest', 'python-dotenv') | Set-Content requirements.txt" in doc
    assert '"`n".Join' not in doc


def test_step1_has_no_openai_when_the_app_calls_no_model():
    doc = _doc(fake_brief(uses_models=False))
    assert "@('pytest') | Set-Content requirements.txt" in doc


# ---------- 2. ASCII dashes in commands, paths, ids ----------

def test_non_breaking_hyphens_become_ascii_in_commands_files_and_code():
    brief = fake_brief(
        setup_commands=[f"xcopy /E /I temp_skills\\docx{NB}edit docx_edit"],
        file_tree=[brain.FileSpec(path=f"Meeting{NB}Minutes.docx", purpose="the output")],
        tasks=[_task(f"Copy `docx{NB}edit` then:\n{FENCE}python\nname = \"Meeting–Minutes.docx\"  # a — b\n{FENCE}")])
    a = _part_a(_doc(brief))
    assert "temp_skills\\docx-edit docx_edit" in a
    assert "`Meeting-Minutes.docx`" in a
    assert "`docx-edit`" in a
    assert 'name = "Meeting-Minutes.docx"  # a - b' in a
    assert not re.search("[‐-―−]", a)


def test_typographic_dashes_stay_in_prose_and_in_the_post():
    brief = fake_brief(goal="A fast tool — really fast.", uses_models=False)
    doc = _doc(brief, post=f"I built it — and it works. {NB}")
    assert "A fast tool — really fast." in _part_a(doc)
    assert "I built it — and it works." in doc.split("# PART B")[1]


def test_model_id_in_the_table_is_ascii():
    idea = dict(IDEA, models_to_run=[f"openrouter:google/gemma{NB}4-26b-a4b-it:free"])
    doc = _doc(fake_brief(uses_models=True), idea)
    assert "| `google/gemma-4-26b-a4b-it:free` |" in doc


# ---------- 3. pip scanner ----------

def test_pip_names_ignore_prose_after_the_command():
    line = "After the file is written, run .\\.venv\\Scripts\\python.exe -m pip install -e . to verify the entry point works."
    assert factgate._pip_names(line) == []
    assert factgate._pip_names("pip install -e . succeeds") == []
    assert factgate._pip_names("pip install -e ./sub/dir") == []


def test_pip_names_keep_real_packages():
    assert factgate._pip_names("pip install requests") == ["requests"]
    assert factgate._pip_names("pip install -U 'openai>=1.0.0' python-docx[x]==1.1 -r req.txt") == ["openai", "python-docx"]
    assert factgate._pip_names("pip install flask and then run it") == ["flask"]
    assert factgate._pip_names("pip install requests.") == ["requests"]
    assert factgate._pip_names("pip install -e . pytest>=8") == ["pytest"]


def test_scan_does_not_flag_prose_or_editable_install():
    text = "run `.\\.venv\\Scripts\\python.exe -m pip install -e .` to verify the entry point works."
    assert not [w for w in factgate.scan_code([("step", text)]) if "installs package" in w]


def test_step_with_editable_install_has_no_prose_package_warnings():
    task = _task("After the file is written, run .\\.venv\\Scripts\\python.exe -m pip install -e . to verify the "
                 "entry point works.", done_when=".\\.venv\\Scripts\\python.exe -m pip install -e . succeeds")
    doc = _doc(fake_brief(tasks=[task]))
    assert "installs package" not in doc


def test_dropped_package_names_are_listed():
    doc = _doc(fake_brief(requirements=["python-docx", "bad name!", "x y"], setup_commands=["git clone https://github.com/a/b"]))
    assert "python-docx" in doc
    assert "2 requested package name(s) were not valid and were left out: `bad name!`, `x y`." in doc


def test_unicode_hyphen_requirement_is_kept_not_dropped():
    doc = _doc(fake_brief(requirements=[f"python{NB}docx"]))
    assert "pip install 'python-docx'" in doc
    assert "were left out" not in doc


# ---------- 4. code fences ----------

def test_code_blocks_use_real_fences_with_a_language_and_no_escaped_hash():
    details = f"Write it:\n{FENCE}python\n# skill_loader.py\nimport os\n{FENCE}\n{FENCE}\nplain\n{FENCE}"
    a = _part_a(_doc(fake_brief(tasks=[_task(details)])))
    assert "'''" not in a
    assert f"{FENCE}python\n# skill_loader.py\nimport os\n{FENCE}" in a
    assert f"{FENCE}text\nplain\n{FENCE}" in a
    assert "\\#" not in a


def test_unclosed_fence_is_closed_and_prose_hash_is_still_escaped():
    a = _part_a(_doc(fake_brief(tasks=[_task(f"# fake heading\n{FENCE}bash\necho hi")])))
    assert "\\# fake heading" in a
    assert f"{FENCE}bash\necho hi\n{FENCE}\n" in a


def test_model_text_cannot_leave_a_block_open_over_the_rest_of_the_file():
    doc = _doc(fake_brief(tasks=[_task(f"{FENCE}python\nx = 1")]))
    assert doc.count(FENCE) % 2 == 0


def test_readme_outline_is_one_block_without_escapes_or_empty_bullets():
    outline = ["# minutes-cli", "A tool.", "", "![demo](docs/demo.gif)", "", "## Quick start",
               f"{FENCE}bash", "git clone https://github.com/x/y.git", "pip install -e .", FENCE, "## Credits"]
    a = _part_a(_doc(fake_brief(readme_outline=outline)))
    block = a.split("## README (public repo)")[1].split("## Known pitfalls")[0]
    assert "````markdown\n# minutes-cli\nA tool.\n\n![demo](docs/demo.gif)" in block
    assert f"## Quick start\n{FENCE}bash\ngit clone" in block
    assert block.rstrip().endswith("````")
    assert "\\#" not in block and "\n- \n" not in block and "\n-\n" not in block
    assert "\U0001f9d1⚠" not in block.split("````markdown")[1]        # no marker inside the README text
    assert "never run them" in block


def test_no_empty_bullets_anywhere():
    doc = _doc(fake_brief(stack=["", "Python"], pitfalls=[" "], tests=["", "t"]))
    assert not re.search(r"^[-*] *$", doc, re.M)


def test_markdown_email_keeps_a_four_backtick_block_whole():
    md = f"````markdown\n# h\n{FENCE}bash\nls\n{FENCE}\nend\n````\n\nafter"
    out = render.markdown_email("hi", md)
    assert out.count("<pre") == 1
    assert "end" in out.split("</pre>")[0] and "<p" in out.split("</pre>")[1]


def test_post_in_part_b_is_verbatim_hashtags_not_escaped():
    doc = _doc(fake_brief(), post="Hook.\n\n#AI #OpenSource")
    assert "\n#AI #OpenSource\n" in doc and "\\#AI" not in doc


# ---------- 5. model id consistency ----------

def test_example_code_uses_the_exact_id_from_the_table():
    idea = dict(IDEA, models_to_run=["openrouter:google/gemma-4-26b-a4b-it:free"])
    details = (f"Use google/gemma{NB}4-26b-a4b-it here:\n{FENCE}python\n"
               f"client.create(model=\"google/gemma-4-26b-a4b-it\")\n{FENCE}")
    doc = _doc(fake_brief(uses_models=True, tasks=[_task(details)]), idea)
    a = _part_a(doc)
    assert 'model="google/gemma-4-26b-a4b-it:free"' in a
    assert "Use google/gemma-4-26b-a4b-it:free here" in a
    assert ":free:free" not in a
    assert "Pass exactly this id as `model=`" in a and "`google/gemma-4-26b-a4b-it:free`" in a


def test_ids_without_suffix_are_left_alone():
    idea = dict(IDEA, models_to_run=["groq:llama-3.3-70b-versatile"])
    a = _part_a(_doc(fake_brief(uses_models=True), idea))
    assert "| `llama-3.3-70b-versatile` |" in a


# ---------- 6. duplicate checks ----------

def test_near_duplicate_checks_are_dropped():
    checks = ["Older model Claude 3.5: make sure the post presents it as older",
              "Older model Claude 3.5. The: make sure the post presents it as older",
              "older model claude 3.5: Make sure the post presents it as older!",
              "Unverified claim in the post: \"it is free.\""]
    doc = _doc(fake_brief(), checks=checks)
    part_b = doc.split("## 7. Check before you post")[1].split("## 8.")[0]
    assert part_b.count("Older model") == 1
    assert "Unverified claim" in part_b


def test_different_checks_are_kept():
    assert render._dedupe(["Verify the star count is 193 k", "Check that the docx-edit skill exists"]) == [
        "Verify the star count is 193 k", "Check that the docx-edit skill exists"]
