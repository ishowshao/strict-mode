from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "Argument",
    "Option",
    "Typer",
    "BadParameter",
    "Exit",
    "echo",
]


class BadParameter(Exception):
    pass


class Exit(Exception):
    def __init__(self, code: int = 0) -> None:
        self.exit_code = code
        super().__init__(code)


class ArgumentInfo:
    def __init__(self, default: Any, help: str | None = None) -> None:
        self.default = default
        self.help = help


class OptionInfo:
    def __init__(self, default: Any, names: Iterable[str], help: str | None = None) -> None:
        self.default = default
        self.names = [name.lstrip("-") for name in names if name.startswith("-")]
        self.help = help
        self.param_name: str | None = None
        self.annotation: Any = inspect._empty

    @property
    def is_flag(self) -> bool:
        return isinstance(self.default, bool)


def Argument(default: Any, *_, help: str | None = None, **__) -> ArgumentInfo:
    return ArgumentInfo(default, help=help)


def Option(default: Any, *names: str, help: str | None = None, **__) -> OptionInfo:
    return OptionInfo(default, names=names, help=help)


_echo_buffer: list[str] | None = None


def echo(message: Any) -> None:
    text = str(message)
    if _echo_buffer is not None:
        _echo_buffer.append(text)
    else:  # pragma: no cover - external CLI usage
        print(text)


class Command:
    def __init__(self, func: Callable[..., Any], name: str | None = None) -> None:
        self.func = func
        self.name = name or func.__name__.replace("_", "-")
        self.signature = inspect.signature(func)
        self.arguments: list[tuple[str, ArgumentInfo]] = []
        self.options_by_name: dict[str, OptionInfo] = {}
        self.option_defaults: dict[str, Any] = {}
        self._prepare()

    def _prepare(self) -> None:
        for param_name, param in self.signature.parameters.items():
            default = param.default
            if isinstance(default, OptionInfo):
                info = default
                info.param_name = param_name
                info.annotation = param.annotation
                aliases = info.names or [param_name.replace("_", "-")]
                for alias in aliases:
                    self.options_by_name[alias] = info
                self.option_defaults[param_name] = info.default
            elif isinstance(default, ArgumentInfo):
                self.arguments.append((param_name, default))
            elif param.default is inspect._empty:
                self.arguments.append((param_name, ArgumentInfo(Ellipsis)))
            else:
                self.option_defaults[param_name] = param.default

    def invoke(self, args: list[str]) -> Any:
        parsed_args: list[Any] = []
        parsed_kwargs: dict[str, Any] = dict(self.option_defaults)

        idx = 0
        positional_tokens: list[str] = []
        while idx < len(args):
            token = args[idx]
            if token.startswith("--"):
                name = token.lstrip("-")
                info = self.options_by_name.get(name)
                if info is None:
                    raise BadParameter(f"Unknown option '--{name}'")
                if info.is_flag:
                    parsed_kwargs[info.param_name or name] = not bool(info.default)
                    idx += 1
                else:
                    idx += 1
                    if idx >= len(args):
                        raise BadParameter(f"Option '--{name}' requires a value")
                    value = args[idx]
                    parsed_kwargs[info.param_name or name] = _convert_value(value, info.annotation, info.default)
                    idx += 1
            else:
                positional_tokens.append(token)
                idx += 1

        required_args = [arg for arg in self.arguments if arg[1].default is Ellipsis]
        if len(positional_tokens) < len(required_args):
            raise BadParameter("Missing required arguments")

        for (param_name, info), token in zip(self.arguments, positional_tokens):
            annotation = self.signature.parameters[param_name].annotation
            default = None if info.default is Ellipsis else info.default
            parsed_args.append(_convert_value(token, annotation, default))

        for (param_name, info) in self.arguments[len(positional_tokens):]:
            if info.default is not Ellipsis:
                parsed_kwargs[param_name] = info.default

        return self.func(*parsed_args, **parsed_kwargs)


def _convert_value(value: str, annotation: Any, default: Any) -> Any:
    target = annotation
    origin = getattr(annotation, "__origin__", None)
    if origin is Optional:
        args = [arg for arg in annotation.__args__ if arg is not type(None)]  # noqa: E721
        target = args[0] if args else str
    if target is inspect._empty or target is None:
        target = type(default) if default is not None else str
    if target is bool:
        return value.lower() in {"true", "1", "yes", "y"}
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    return value


class Typer:
    def __init__(self, help: str | None = None) -> None:
        self.help = help
        self._commands: dict[str, Command] = {}

    def command(self, name: str | None = None):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            command = Command(func, name=name)
            self._commands[command.name] = command
            return func

        return decorator

    def get_command(self, name: str) -> Command:
        if name not in self._commands:
            raise BadParameter(f"Command '{name}' not found")
        return self._commands[name]

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("CLI execution not supported in minimal Typer")


class Result:
    def __init__(self, exit_code: int, output: str) -> None:
        self.exit_code = exit_code
        self.output = output


class CliRunner:
    def invoke(self, app: Typer, args: list[str], catch_exceptions: bool = True) -> Result:
        global _echo_buffer
        _echo_buffer = []
        try:
            command_name = args[0]
            cmd_args = args[1:]
            command = app.get_command(command_name)
            command.invoke(cmd_args)
            exit_code = 0
        except Exit as exc:
            exit_code = exc.exit_code
        except BadParameter as exc:
            exit_code = 2
            _echo_buffer.append(str(exc))
        finally:
            output_lines = _echo_buffer or []
            _echo_buffer = None
        return Result(exit_code=exit_code, output="\n".join(output_lines) + ("\n" if output_lines else ""))


testing = SimpleNamespace(CliRunner=CliRunner)
