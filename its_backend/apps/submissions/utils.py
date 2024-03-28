import json
from ast import literal_eval
from enum import Enum

from rest_framework.exceptions import APIException

from ..accounts.models import Teaches
from ..questions.models import Question, TestCase
from ..submissions.its_utils import ITSFeedbackException, ITSParserException, ITSInterpreterException
from .its_utils import (
    its_request_feedback_fix,
    its_request_feedback_hint,
    its_request_interpreter,
    its_request_parser,
)
from .models import Submissiondata


class ItsStatus(Enum):
    ITS_SUCCESS = "ITS SUCCESS"
    ITS_STUDENT_SUBMISSION_PARSER_FAILURE = "The ITS failed to parse Student Program. "
    ITS_REF_PROGRAM_PARSER_FAILURE = "The ITS failed to parse Reference Program. "
    ITS_FEEDBACK_INTERPRETER_FAILURE = (
        "The ITS interpreter fails to interprete the Program and Testcases. "
    )
    ITS_FEEDBACK_HINT_FAILURE = "The ITS fails to provide Feedback Hint. "
    ITS_FEEDBACK_FIX_FAILURE = "The ITS fails to provide Feedback Fix. "
    ITS_STUDENT_SUBMISSION_PROGRAM_INVALID = (
        "The ITS parser failed to evaluate the Student Submission Program"
    )


class QuestionNotFoundError(Exception):
    pass


class QuestionNotAvailableToStudentError(Exception):
    pass


class CannotGeneratedFeedbackException(APIException):
    default_detail = (
        "Program too complex to cannot be processed by ITS feedback service"
    )


def get_parsed_ref_program(question):
    try:
        ref_program = question.ref_program
        language = question.language.lower()
        ref_program = ref_program.replace("\\n", "\n").replace("\\t", "\t")
        parsed_program = its_request_parser(language, ref_program, "Reference Program")
        return parsed_program
    except ITSParserException:
        return None


def get_parsed_stu_program(program, language):
    try:
        program = program.replace("\\n", "\n").replace("\\t", "\t")
        parsed_program = its_request_parser(language, program, "Student Submission")
        return parsed_program
    except ITSParserException:
        return None


def compute_score(qn_id, language, student_solution, function):
    test_cases = TestCase.objects.filter(question_id=qn_id)
    total_score = test_cases.count()
    score = 0
    failed_test_cases = []
    for test_case in test_cases:
        inputs = ""
        arguments = "[" + str(test_case.input) + "]"
        its_interpreter_response = its_request_interpreter(
            language, student_solution, function, inputs, arguments
        )
        if not its_interpreter_response:
            continue
        result = its_interpreter_response["entries"][-1]["mem"]["$ret'"]
        result = str(result)
        if literal_eval(result) == literal_eval(test_case.output):
            score += 1
        else:
            failed_test_cases.append(test_case.pk)
    return total_score, score, failed_test_cases


def get_submission_number(user, qn_id):
    submissions = Submissiondata.objects.filter(submitted_by=user, qn_id=qn_id)
    return submissions.count() + 1


def get_failed_test_case_arg(failed_test_cases):
    sample_test_case = TestCase.objects.filter(pk__in=failed_test_cases)
    if sample_test_case.count() == 0:
        return ""
    else:  # noqa: RET505
        return sample_test_case[0].input


def process_feedback_params(
    language, parsed_ref_program, parsed_stu_program, failed_test_cases
):
    io_input = "[]"
    arguments = "[" + str(get_failed_test_case_arg(failed_test_cases)) + "]"
    language = language if language.lower() != "python" else "py"
    parsed_ref_program = json.dumps(parsed_ref_program)
    parsed_stu_program = json.dumps(parsed_stu_program)
    return io_input, arguments, language, parsed_ref_program, parsed_stu_program


def get_feedback_for_tutor(
    language, parsed_ref_program, parsed_stu_program, function, failed_test_cases
):
    if not failed_test_cases:
        raise CannotGeneratedFeedbackException()
    
    io_input, arguments, language, parsed_ref_program, parsed_stu_program = (
        process_feedback_params(
            language, parsed_ref_program, parsed_stu_program, failed_test_cases
        )
    )
    try:
        feedback_fix_array = its_request_feedback_fix(
            language,
            parsed_ref_program,
            parsed_stu_program,
            function,
            io_input,
            arguments,
        )
        its_feedback_fix_tutor = {"fixes": feedback_fix_array}
        return json.dumps(its_feedback_fix_tutor)
    except ITSFeedbackException as err:
        raise CannotGeneratedFeedbackException() from err


def get_feedback_for_student(
    language, parsed_ref_program, parsed_stu_program, function, failed_test_cases
):
    if not failed_test_cases:
        raise CannotGeneratedFeedbackException()
    
    io_input, arguments, language, parsed_ref_program, parsed_stu_program = (
        process_feedback_params(
            language, parsed_ref_program, parsed_stu_program, failed_test_cases
        )
    )
    try:
        feedback_hint_array = its_request_feedback_hint(
            language,
            parsed_ref_program,
            parsed_stu_program,
            function,
            io_input,
            arguments,
        )
        its_feedback_hint_student = {"hints": feedback_hint_array}
        return json.dumps(its_feedback_hint_student)
    except ITSFeedbackException as err:
        raise CannotGeneratedFeedbackException() from err


def check_is_question_accessible(request, question):
    # check if student has access to the question
    tutors = Teaches.objects.filter(student_id=request.user.pk).values_list(
        "tutor_id", flat=True
    )
    question_pub_by = question.pub_by.pk
    result = question_pub_by in tutors
    return result


def process_submission_request(request):
    language = request.data.get("language")
    program = request.data.get("program")
    qn_id = request.data.get("qn_id")
    status = ""

    try:
        question = Question.objects.get(pk=qn_id)
    except Question.DoesNotExist:
        raise QuestionNotFoundError(f"Question with qn_id {qn_id} not found") from None

    # check if student can submit to question
    if not check_is_question_accessible(request, question):
        raise QuestionNotAvailableToStudentError(
            f"Question with qn_id {qn_id} is not available to the student"
        ) from None

    mutable_data = request.data.copy()
    # parsed student and reference program
    parsed_stu_program = get_parsed_stu_program(program, language)
    parsed_ref_program = get_parsed_ref_program(question)

    # parse both ref program and student program successfully
    if parsed_ref_program and parsed_stu_program:
        # the entry function of the program
        if not parsed_stu_program["fncs"]:
            status += ItsStatus.ITS_STUDENT_SUBMISSION_PROGRAM_INVALID.value
            total_score = 0
            score = 0
            its_feedback_hint_student = {"message": ""}
            its_feedback_hint_student = json.dumps(its_feedback_hint_student)
            its_feedback_fix_tutor = {"message": ""}
            its_feedback_fix_tutor = json.dumps(its_feedback_fix_tutor)
            test_cases = TestCase.objects.filter(question_id=qn_id)
            total_score = test_cases.count()
        else:
            function = next(iter(parsed_stu_program["fncs"].keys()))
            # number of test cases passed
            try:
                total_score, score, failed_test_cases = compute_score(
                    qn_id, language, parsed_stu_program, function
                )
            except ITSInterpreterException:
                test_cases = TestCase.objects.filter(question_id=qn_id)
                total_score = test_cases.count()
                total_score = 0
                score = 0
                failed_test_cases = None
                status += ItsStatus.ITS_FEEDBACK_INTERPRETER_FAILURE.value

            try:
                its_feedback_fix_tutor = get_feedback_for_tutor(
                    language,
                    parsed_ref_program,
                    parsed_stu_program,
                    function,
                    failed_test_cases,
                )
                status += ItsStatus.ITS_SUCCESS.value
            except CannotGeneratedFeedbackException:
                its_feedback_fix_tutor = {"message": ""}
                its_feedback_fix_tutor = json.dumps(its_feedback_fix_tutor)
                status += ItsStatus.ITS_FEEDBACK_FIX_FAILURE.value

            try:
                its_feedback_hint_student = get_feedback_for_student(
                    language,
                    parsed_ref_program,
                    parsed_stu_program,
                    function,
                    failed_test_cases,
                )
            except CannotGeneratedFeedbackException:
                its_feedback_hint_student = {"message": ""}
                its_feedback_hint_student = json.dumps(its_feedback_hint_student)
                status += ItsStatus.ITS_FEEDBACK_HINT_FAILURE.value

    # its parse refprogram/ stundet program failed
    else:
        total_score = 0
        score = 0
        its_feedback_hint_student = {"message": ""}
        its_feedback_hint_student = json.dumps(its_feedback_hint_student)
        its_feedback_fix_tutor = {"message": ""}
        its_feedback_fix_tutor = json.dumps(its_feedback_fix_tutor)
        test_cases = TestCase.objects.filter(question_id=qn_id)
        total_score = test_cases.count()
        if not parsed_stu_program:
            status += ItsStatus.ITS_STUDENT_SUBMISSION_PARSER_FAILURE.value
        if not parsed_ref_program:
            status += ItsStatus.ITS_REF_PROGRAM_PARSER_FAILURE.value

    # get submission number
    submission_number = get_submission_number(request.user, qn_id)

    # reform the request data
    mutable_data["qn_id"] = qn_id
    mutable_data["total_score"] = total_score
    mutable_data["score"] = score
    mutable_data["submission_number"] = submission_number
    mutable_data["its_feedback_hint_student"] = its_feedback_hint_student
    mutable_data["its_feedback_fix_tutor"] = its_feedback_fix_tutor
    mutable_data["status"] = status

    return mutable_data
