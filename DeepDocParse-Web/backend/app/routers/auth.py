"""用户注册/登录（JWT）。TODO: users 表 + bcrypt + jose。"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/register")
async def register():
    raise HTTPException(status_code=501, detail="TODO")


@router.post("/login")
async def login():
    raise HTTPException(status_code=501, detail="TODO")
