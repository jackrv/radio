"""Кодові команди «слово 42 42»: склеювання номера й упізнавання за номером."""

from callsigns import COMMAND_ANCHORED_THRESHOLD, Callsigns, command_dictionary


def pairs(found):
    return [(name, num) for name, num, _score in found]


def test_dictionary_line_with_spoken_number_halves():
    commands = command_dictionary(["злива 42 42", "грім 0 22"])
    assert commands.numbers["злива"] == {"4242"}
    assert commands.numbers["грім"] == {"022"}


def test_number_split_by_comma_is_joined_when_allowed():
    commands = command_dictionary(["злива 4242", "грім 022"])
    assert pairs(commands.find("злива, 42, 42, прийом")) == [("злива", "4242")]
    assert pairs(commands.find("Грім, 0, 22")) == [("грім", "022")]
    assert pairs(commands.find("злива 42 42")) == [("злива", "4242")]


def test_allowed_first_group_wins_over_joining():
    callsigns = Callsigns.from_lines(["дуб: 42 4242"])
    assert pairs(callsigns.find("дуб 42, 42")) == [("дуб", "42")]


def test_join_requires_joined_number_in_dictionary():
    callsigns = Callsigns.from_lines(["дуб: 15"])
    assert pairs(callsigns.find("дуб, 16, 16")) == []


def test_exact_number_lets_less_similar_word_match_command():
    commands = command_dictionary(["злива 4242"])
    assert pairs(commands.find("Злата 4242, прийом")) == [("злива", "4242")]
    assert pairs(commands.find("злата, 42, 42")) == [("злива", "4242")]


def test_anchoring_needs_exact_number():
    commands = command_dictionary(["злива 4242"])
    assert commands.find("злата 42") == []
    assert commands.find("злата 4243") == []


def test_anchoring_is_only_for_commands():
    callsigns = Callsigns.from_lines(["злива 4242"])
    assert callsigns.find("злата 4242") == []


def test_anchoring_picks_most_similar_owner_of_shared_number():
    commands = command_dictionary(["злива 4242", "грім 4242"])
    assert pairs(commands.find("гріб 4242")) == [("грім", "4242")]


def test_anchoring_rejects_word_at_fifty():
    assert COMMAND_ANCHORED_THRESHOLD > 50
    commands = command_dictionary(["злива 4242"])
    assert commands.find("зав 4242") == []


def test_matching_word_with_other_number_falls_back_to_owner_of_number():
    # «грім 2727» — у «грім» інші номери; 2727 належить схожому «гріб».
    commands = command_dictionary(["грім 222", "гріб 2727"])
    assert pairs(commands.find("грім, 27,27")) == [("гріб", "2727")]
    assert pairs(commands.find("грім 222")) == [("грім", "222")]


def test_near_number_rules():
    from callsigns import near_number
    assert near_number("023", {"022", "777"}) == "022"
    assert near_number("922", {"022"}) is None, "перша цифра має збігатися"
    assert near_number("033", {"022"}) is None, "відрізняється більше ніж однією цифрою"
    assert near_number("02", {"03"}) is None, "лише від трьох цифр"
    assert near_number("4243", {"4242", "4244"}) is None, "кілька кандидатів"


def test_command_number_with_one_wrong_digit():
    commands = command_dictionary(["грім 022", "грім 777"])
    assert pairs(commands.find("Грім, 0,23, грім 0,23")) == [("грім", "022")]
    assert pairs(commands.find("грім 028")) == [("грім", "022")]
    assert pairs(commands.find("грім 922")) == []
    assert pairs(commands.find("грім 022")) == [("грім", "022")]


def test_near_number_needs_confident_word_and_is_only_for_commands():
    assert pairs(command_dictionary(["грім 022"]).find("гріб 023")) == []
    assert pairs(Callsigns.from_lines(["дуб: 422"]).find("дуб 423")) == []
