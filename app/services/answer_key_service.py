from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import MockExam, ProblemGroup, Question


@dataclass(frozen=True)
class QuestionValidationIssue:
    question_id: int
    display_label: str
    message: str


def generate_continuous_questions(
    db: Session,
    exam: MockExam,
    total_questions: int,
    choice_count: int,
    physical_sheet_id: int,
    scoring_section_id: int | None,
) -> int:
    if total_questions < 1:
        raise ValueError("Total questions must be at least 1.")
    _validate_choice_count(choice_count)
    _clear_existing_questions_for_sheet(exam, physical_sheet_id)
    for index in range(1, total_questions + 1):
        exam.questions.append(
            Question(
                physical_sheet_id=physical_sheet_id,
                scoring_section_id=scoring_section_id,
                question_no=index,
                local_question_no=index,
                local_question_label=str(index),
                display_label=str(index),
                choice_count=choice_count,
                display_order=index,
            )
        )
    exam.numbering_mode = "continuous"
    db.commit()
    return total_questions


def generate_grouped_questions(
    db: Session,
    exam: MockExam,
    group_counts: list[int],
    choice_count: int,
    physical_sheet_id: int,
    scoring_section_id: int | None,
) -> int:
    if not group_counts:
        raise ValueError("Grouped tests need at least one 問題.")
    if any(count < 1 for count in group_counts):
        raise ValueError("Each 問題 needs at least 1 問.")
    _validate_choice_count(choice_count)
    _clear_existing_questions_for_sheet(exam, physical_sheet_id)
    display_order = 1
    for problem_index, subquestion_count in enumerate(group_counts, start=1):
        exam.problem_groups.append(
            ProblemGroup(
                physical_sheet_id=physical_sheet_id,
                problem_no=problem_index,
                problem_title=f"問題{problem_index}",
                subquestion_count=subquestion_count,
                display_order=problem_index,
            )
        )
        for subquestion_no in range(1, subquestion_count + 1):
            exam.questions.append(
                Question(
                    physical_sheet_id=physical_sheet_id,
                    scoring_section_id=scoring_section_id,
                    question_no=display_order,
                    problem_no=problem_index,
                    subquestion_no=subquestion_no,
                    local_question_no=subquestion_no,
                    local_question_label=str(subquestion_no),
                    display_label=f"問題{problem_index}-問{subquestion_no}",
                    choice_count=choice_count,
                    display_order=display_order,
                )
            )
            display_order += 1
    exam.numbering_mode = "reset_per_problem"
    db.commit()
    return display_order - 1


def apply_problem_group_counts_to_sheet(
    db: Session,
    exam: MockExam,
    physical_sheet_id: int,
    group_counts: list[int],
) -> int:
    if not group_counts:
        raise ValueError("Grouped tests need at least one 問題.")
    if any(count < 1 for count in group_counts):
        raise ValueError("Each 問題 needs at least 1 問.")

    sheet_questions = sorted(
        [
            question
            for question in exam.questions
            if question.physical_sheet_id == physical_sheet_id
        ],
        key=lambda question: (question.display_order or 0, question.question_no or 0),
    )
    if not sheet_questions:
        raise ValueError("Selected sheet has no questions to group.")
    expected_total = sum(group_counts)
    if expected_total != len(sheet_questions):
        raise ValueError(
            f"Problem counts total {expected_total}, but this sheet has {len(sheet_questions)} questions."
        )

    for group in list(exam.problem_groups):
        if group.physical_sheet_id == physical_sheet_id:
            exam.problem_groups.remove(group)

    question_index = 0
    for problem_index, subquestion_count in enumerate(group_counts, start=1):
        exam.problem_groups.append(
            ProblemGroup(
                physical_sheet_id=physical_sheet_id,
                problem_no=problem_index,
                problem_title=f"問題{problem_index}",
                subquestion_count=subquestion_count,
                display_order=problem_index,
            )
        )
        for subquestion_no in range(1, subquestion_count + 1):
            question = sheet_questions[question_index]
            question.problem_no = problem_index
            question.subquestion_no = subquestion_no
            question.local_question_no = subquestion_no
            question.local_question_label = str(subquestion_no)
            question.display_label = f"問題{problem_index}-問{question.local_question_label}"
            question_index += 1

    exam.numbering_mode = "reset_per_problem"
    db.commit()
    return expected_total


def save_answer_key(
    db: Session, exam: MockExam, answers: dict[int, str]
) -> list[QuestionValidationIssue]:
    question_by_id = {question.id: question for question in exam.questions}
    for question_id, answer in answers.items():
        if question_id in question_by_id:
            question_by_id[question_id].correct_answer = answer.strip() or None
    issues = validate_answer_key(exam)
    if not issues and exam.students:
        exam.status = "ready_to_print"
    db.commit()
    return issues


def validate_answer_key(exam: MockExam) -> list[QuestionValidationIssue]:
    issues: list[QuestionValidationIssue] = []
    for question in exam.questions:
        if not question.correct_answer:
            issues.append(_issue(question, "Correct answer is required."))
            continue
        valid_answers = {str(number) for number in range(1, question.choice_count + 1)}
        if question.correct_answer not in valid_answers:
            issues.append(_issue(question, "Correct answer must be within the choice range."))
        if question.physical_sheet_id is None:
            issues.append(_issue(question, "Physical sheet is required."))
        if exam.assessment_type != "peer_review" and question.scoring_section_id is None:
            issues.append(_issue(question, "Scoring section is required."))
    return issues


def _clear_existing_questions_for_sheet(exam: MockExam, physical_sheet_id: int) -> None:
    exam.questions[:] = [
        question for question in exam.questions if question.physical_sheet_id != physical_sheet_id
    ]
    exam.problem_groups[:] = [
        group for group in exam.problem_groups if group.physical_sheet_id != physical_sheet_id
    ]


def _validate_choice_count(choice_count: int) -> None:
    if choice_count < 2 or choice_count > 5:
        raise ValueError("Choices must be between 2 and 5.")


def _issue(question: Question, message: str) -> QuestionValidationIssue:
    return QuestionValidationIssue(
        question_id=question.id,
        display_label=question.display_label,
        message=message,
    )
