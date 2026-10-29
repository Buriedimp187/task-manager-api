import pytest
from pydantic import ValidationError

from app.schemas import TaskCreate, TaskStatus, TaskUpdate


def test_create_applies_defaults():
    task = TaskCreate(title="Write tests")

    assert task.description is None
    assert task.status == TaskStatus.todo


def test_create_strips_whitespace_from_title():
    assert TaskCreate(title="  Write tests  ").title == "Write tests"


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_create_rejects_invalid_title(title):
    with pytest.raises(ValidationError):
        TaskCreate(title=title)


def test_create_rejects_missing_title():
    with pytest.raises(ValidationError):
        TaskCreate()


@pytest.mark.parametrize("title", ["\x00", "bad\x00title", "\x00 leading", "trailing \x00"])
def test_create_rejects_nul_characters_in_the_title(title):
    with pytest.raises(ValidationError, match="NUL"):
        TaskCreate(title=title)


@pytest.mark.parametrize("description", ["\x00", "bad\x00text", "text\x00"])
def test_create_rejects_nul_characters_in_the_description(description):
    with pytest.raises(ValidationError, match="NUL"):
        TaskCreate(title="Write tests", description=description)


def test_update_rejects_nul_characters():
    with pytest.raises(ValidationError, match="NUL"):
        TaskUpdate(title="bad\x00title")
    with pytest.raises(ValidationError, match="NUL"):
        TaskUpdate(description="bad\x00text")


def test_nul_rejection_does_not_affect_other_control_characters_or_unicode():
    task = TaskCreate(title="tab\there é中\U0001f600", description="line1\nline2")

    assert task.title == "tab\there é中\U0001f600"
    assert task.description == "line1\nline2"


def test_error_response_shape():
    from app.schemas import ErrorResponse

    assert ErrorResponse(detail="Task not found").model_dump() == {"detail": "Task not found"}
    with pytest.raises(ValidationError):
        ErrorResponse()


def test_create_rejects_unknown_status():
    with pytest.raises(ValidationError):
        TaskCreate(title="Write tests", status="archived")


def test_create_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        TaskCreate(title="Write tests", id=1)


def test_create_rejects_overlong_description():
    with pytest.raises(ValidationError):
        TaskCreate(title="Write tests", description="x" * 2001)


@pytest.mark.parametrize("description", ["", "   ", "\n\t "])
def test_create_normalizes_blank_description_to_none(description):
    assert TaskCreate(title="Write tests", description=description).description is None


def test_create_trims_description():
    assert TaskCreate(title="Write tests", description="  Cover edge cases \n").description == (
        "Cover edge cases"
    )


def test_create_keeps_explicit_null_description():
    assert TaskCreate(title="Write tests", description=None).description is None


def test_create_measures_description_length_after_trimming():
    padded = "  " + "x" * 2000 + "  "

    assert TaskCreate(title="Write tests", description=padded).description == "x" * 2000


@pytest.mark.parametrize("description", ["", "   "])
def test_update_normalizes_blank_description_to_none(description):
    update = TaskUpdate(description=description)

    assert update.model_dump(exclude_unset=True) == {"description": None}


def test_update_trims_description():
    update = TaskUpdate(description="  New text  ")

    assert update.model_dump(exclude_unset=True) == {"description": "New text"}


def test_update_distinguishes_omitted_description_from_explicit_null():
    omitted = TaskUpdate(title="New title")
    explicit_null = TaskUpdate(title="New title", description=None)

    assert "description" not in omitted.model_dump(exclude_unset=True)
    assert explicit_null.model_dump(exclude_unset=True)["description"] is None


def test_update_accepts_a_single_field():
    update = TaskUpdate(status="done")

    assert update.model_dump(exclude_unset=True) == {"status": TaskStatus.done}


def test_update_allows_clearing_description():
    update = TaskUpdate(description=None)

    assert update.model_dump(exclude_unset=True) == {"description": None}


def test_update_rejects_empty_body():
    with pytest.raises(ValidationError):
        TaskUpdate()


@pytest.mark.parametrize("field", ["title", "status"])
def test_update_rejects_null_for_required_fields(field):
    with pytest.raises(ValidationError):
        TaskUpdate(**{field: None})


def test_update_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        TaskUpdate(title="New title", created_at="2026-01-01T00:00:00")
