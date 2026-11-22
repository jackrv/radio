"""Словникові блоки: точні команди та підтверджені контекстні виправлення номерів.

Немає вибору «найближчого» числа. Для команди потрібна незмінена повна група
її коду й єдиний словниковий варіант; для позивного — незалежні еталони.
"""
import re

import core.callsigns as names_module
from core.callsigns import FOLD, NUMBER_WORDS, basic_normalize, words_to_digits

TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|[0-9]+")
_PROTECTED = re.compile(r'\b[0-9]+[:./-][0-9]+\b|https?://',re.I)
_GAP = re.compile(r'[ \t,]+')


def command_groups(commands,name,code):
    explicit=getattr(commands,'code_groups',{}).get((name,code))
    if explicit:return tuple(explicit)
    if len(code)==3 and code.startswith('0'):return (code[:1],code[1:])
    return (code[:2],code[2:]) if len(code)==4 else (code,)


def callsign_lookup(callsigns):
    if callsigns is None:return {}
    names=set(callsigns.names)
    lookup={alias.translate(FOLD):target for alias,target in names_module.CALLSIGN_ALIASES.items() if target in names}
    lookup.update((name.translate(FOLD),name) for name in names)
    return lookup


def callsign_name(word,callsigns,lookup=None):
    """Лише точне ім'я, складена форма «я+ім'я» або явно заданий аліас."""
    if callsigns is None:return None,False
    folded=basic_normalize(word).translate(FOLD)
    lookup=callsign_lookup(callsigns) if lookup is None else lookup
    for value,joined_self in ((folded,False),(folded[1:],True)):
        if joined_self and not folded.startswith('я'):continue
        name=lookup.get(value)
        if name:return name,joined_self
    return None,False


def _editable(text,token):
    if token.start() and (text[token.start()-1].isalnum() or text[token.start()-1]=='_'):return False
    prefix=text[:token.start()]
    return prefix.count('«')<=prefix.count('»') and prefix.count('"')%2==0


# «буря нуль 11» Whisper пише «буряна 11»: «нуль» зливається з назвою команди.
# 23.11.2026: 11 записів «<назва>на» при еталоні «<назва> 0 …», жодного «<назва>на» в еталонах.
_GLUED_ZERO = ("на",)


def split_glued_zero(text,callsigns,commands):
    """«<команда>на 11» → «<команда> 0 11», лише якщо «0»+почуті цифри — точний код цієї команди.

    Без цифр — «<команда> 0», лише коли код на нуль у команди єдиний.
    """
    if commands is None:return text,[]
    tokens=list(TOKEN.finditer(text));edits=[];changes=[]
    callsign_names=set(getattr(callsigns,'names',()) or ())
    for i,token in enumerate(tokens[:-1]):
        if not _editable(text,token) or token.group().isdigit():continue
        word=basic_normalize(token.group())
        if word in commands.names or word in callsign_names:continue
        name=next((word[:-len(s)] for s in _GLUED_ZERO if word.endswith(s) and word[:-len(s)] in commands.names),None)
        if not name or name in callsign_names or name in commands.any_number:continue
        digits=[];end=token.end()
        for item in tokens[i+1:]:
            if not item.group().isdigit() or not _GAP.fullmatch(text[end:item.start()]):break
            if item.end()<len(text) and (text[item.end()].isalnum() or text[item.end()]=='_'):break
            digits.append(item.group());end=item.end()
        allowed={code for code in commands.numbers.get(name,()) if code.isascii() and code.isdigit()}
        if not digits:
            # Без цифр — лише «<команда> 0», якщо код на нуль єдиний; решту дописує command_completion
            # з позначкою «почуто» (погоджено власником 23.11.2026).
            if len([code for code in allowed if code.startswith('0') and len(code)>1])!=1:continue
        elif '0'+''.join(digits) not in allowed and '0'+digits[0] not in allowed:continue
        edits.append((token.start(),token.end(),name+' 0'))
        changes.append(dict(kind='glued_zero',name=name,heard=token.group(),expected=name+' 0'))
    for start,end,value in reversed(edits):text=text[:start]+value+text[end:]
    return text,changes


def constrain_dictionary_text(text,callsigns,commands):
    if not text or _PROTECTED.search(text):return text,[]
    text,changes=split_glued_zero(text,callsigns,commands)
    lookup=callsign_lookup(callsigns)
    # Число словами після відомого імені: змінюється запис, а не значення.
    tokens=list(TOKEN.finditer(text));edits=[]
    for i,token in enumerate(tokens[:-1]):
        if not _editable(text,token):continue
        name=basic_normalize(token.group())
        cs,_=callsign_name(token.group(),callsigns,lookup)
        dictionary=commands if commands is not None and name in commands.names else callsigns if cs else None
        if dictionary is None:continue
        name=name if dictionary is commands else cs
        parts=[];end=token.end();has_words=False
        for item in tokens[i+1:]:
            gap=text[end:item.start()]
            if not _GAP.fullmatch(gap) or (parts and ',' in gap):break
            word=basic_normalize(item.group())
            if not item.group().isdigit() and word not in NUMBER_WORDS:break
            parts.append(word);has_words |= not item.group().isdigit();end=item.end()
        if not parts or not has_words:continue
        value=words_to_digits(parts);allowed=dictionary.numbers.get(name,())
        if value not in allowed and name not in dictionary.any_number and not (dictionary is commands and value*2 in allowed):continue
        start=tokens[i+1].start();edits.append((start,end,value))
        changes.append(dict(kind='number_words',heard=text[start:end],expected=value))
    for start,end,value in reversed(edits):text=text[:start]+value+text[end:]

    tokens=list(TOKEN.finditer(text));edits=[]
    for i,token in enumerate(tokens[:-1]):
        if not _editable(text,token):continue
        name=basic_normalize(token.group())
        if commands is not None and name in commands.names and (callsigns is None or name not in callsigns.names):
            if name in commands.any_number:continue
            allowed=commands.numbers.get(name,());digits=[];ends=[];end=token.end()
            for item in tokens[i+1:]:
                if not item.group().isdigit() or not _GAP.fullmatch(text[end:item.start()]):break
                if item.end()<len(text) and (text[item.end()].isalnum() or text[item.end()]=='_'):break
                digits.append(item.group());ends.append(item.end());end=item.end()
                if len(''.join(digits))>=9:break
            if not digits:continue
            candidates=[];exact=[];forms=[]
            for code in allowed:
                if not code.isascii() or not code.isdigit():continue
                groups=command_groups(commands,name,code);length=len(code)
                used=next((j+1 for j in range(len(digits)) if len(''.join(digits[:j+1]))==length),None)
                if used is None:
                    # Втрачений провідний нуль: «0 22» почуто як «22» — та сама група, лише форма коду,
                    # не найближче число. 22.11.2026: 7 вірних, 0 гірших. Половину подвоєної пари
                    # («42» при 4242) відновлює core.command_completion із позначкою «почуто».
                    if code=='0'+digits[0]:
                        forms.append((tokens[i+1].start(),ends[0],' '.join(command_groups(commands,name,code))))
                    continue
                heard=''.join(digits[:used]);target=' '.join(groups)
                if heard==code:exact.append((tokens[i+1].start(),ends[used-1],target));continue
                if len(groups)<2:continue
                split=[];at=0
                for group in groups:split.append(heard[at:at+len(group)]);at+=len(group)
                # Одна ціла група вже почута правильно; відновлюється цілий словниковий блок.
                if any(a==b for a,b in zip(groups,split)):
                    candidates.append((tokens[i+1].start(),ends[used-1],target))
            chosen=(max(exact,key=lambda item:item[1]) if exact else candidates[0] if len(candidates)==1
                    else forms[0] if not candidates and len(forms)==1 else None)
            if chosen and text[chosen[0]:chosen[1]]!=chosen[2]:
                edits.append(chosen);changes.append(dict(kind='command_block',name=name,heard=text[chosen[0]:chosen[1]],expected=chosen[2]))
        else:
            name,joined_self=callsign_name(token.group(),callsigns,lookup)
            if not name or (commands is not None and name in commands.names):continue
            number=tokens[i+1]
            if not number.group().isdigit() or not _GAP.fullmatch(text[token.end():number.start()]):continue
            if number.end()<len(text) and (text[number.end()].isalnum() or text[number.end()]=='_'):continue
            if i+2<len(tokens) and tokens[i+2].group().isdigit() and _GAP.fullmatch(text[number.end():tokens[i+2].start()]):continue
            old=number.group();rule=getattr(callsigns,'number_rules',{}).get((name,old))
            if not rule or name in callsigns.any_number or old in callsigns.numbers.get(name,()):continue
            target=rule['target']
            if target not in callsigns.numbers.get(name,()):continue
            replacement=('я ' if joined_self else '')+name+' '+target
            edits.append((token.start(),number.end(),replacement))
            changes.append(dict(kind='callsign_number',name=name,heard=old,expected=target,sources=rule['sources']))
    for start,end,value in reversed(edits):text=text[:start]+value+text[end:]
    return text,changes
