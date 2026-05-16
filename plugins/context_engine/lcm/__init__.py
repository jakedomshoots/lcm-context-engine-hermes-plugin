from .engine import LCMContextEngine


def register(ctx):
    ctx.register_context_engine(LCMContextEngine())
