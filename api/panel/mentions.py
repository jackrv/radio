"""Вибірки оператора, доступні лише авторизованій локальній панелі."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal


class MentionQuery(BaseModel):
    terms: str = Field(max_length=3030)
    since: float
    until: float
    order: Literal['old','new','unreviewed']='old'


def register(app, ctx):
    @app.post('/api/mentions')
    def mentions(payload: MentionQuery, admin=Depends(ctx.actor)):
        from shared.mention_selection import find, save_selection
        try:
            result = find(ctx.recordings.root, payload.terms, payload.since, payload.until, order=payload.order)
            selection = save_selection(ctx.db_path, result)
            return {**{k:v for k,v in result.items() if k!='ids'}, 'selection': selection}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/mentions')
    def mention_page(selection: str, offset: int=0, limit: int=100,
                     order: Literal['old','new','unreviewed'] | None=None, admin=Depends(ctx.actor)):
        from shared.mention_selection import load_selection, page_selection
        try:
            saved=load_selection(ctx.db_path,selection)
            return {**page_selection(ctx.recordings.root,saved,offset,limit,order=order),'selection':selection}
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
