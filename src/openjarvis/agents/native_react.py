"""NativeReActAgent -- Thought-Action-Observation loop agent.

Renamed from ``ReActAgent`` to clarify this is OpenJarvis's native
implementation, not an integration with an external project.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, ToolUsingAgent
from openjarvis.agents.prompt_loader import (
    load_few_shot_exemplars,
    load_system_prompt_override,
)
from openjarvis.core.events import EventBus
from openjarvis.core.registry import AgentRegistry
from openjarvis.core.types import Message, Role, ToolCall, ToolResult, _message_to_dict
from openjarvis.engine._stubs import InferenceEngine
from openjarvis.tools._stubs import BaseTool, build_tool_descriptions

REACT_SYSTEM_PROMPT = """\
You are Jarvis, a precise and truthful ReAct agent. For each step, respond with exactly one of:

1. To think and act:
Thought: <your reasoning>
Action: <tool_name>
Action Input: <json arguments>

2. To give a final answer:
Thought: <your reasoning>
Final Answer: <your answer>

# Critical Grounding & Anti-Hallucination Directives
- NEVER claim or pretend that an external action was completed (e.g. creating/updating a calendar event, creating a task, sending an email, modifying a file, or running a system command) unless you actually invoked the corresponding tool and observed a successful result in the Observation during this turn.
- If you do not have a tool to perform a requested real-world action, or if a tool is missing or unauthorized, state the limitation or error truthfully in your Final Answer. Never invent fake success confirmations, simulated results, or imaginary links.
- Your capabilities are strictly bounded by the tools listed below. If an action cannot be performed with the available tools, tell the user immediately.

# Using Skills

Tools whose names begin with `skill_` are SKILLS. When you call a skill tool,
the response can take one of two forms:

- **Computed result**: The skill ran a deterministic pipeline and returned a
  value (number, string, JSON, etc.). Use the value directly in your answer.

- **Procedural instructions**: The skill returned markdown text describing
  HOW to accomplish a task. Recognize this when the response starts with
  `#` headings, contains bullet lists, or uses phrases like "When asked
  to...", "First...", "Steps:". When you receive instructions:
  1. READ the instructions carefully — they are your playbook
  2. FOLLOW the steps using your OTHER tools (e.g. calculator, web_search,
     shell_exec, file_read) — not the same skill
  3. DO NOT call the same skill again — you already have its instructions
  4. Synthesize a Final Answer from what you learned

{skill_examples}{tool_descriptions}"""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError("Non-JSON numeric constant")


def _parse_json_response(text: str) -> dict[str, str]:
    """Parse a complete JSON envelope without recovering partial tool calls."""
    result = {"thought": "", "action": "", "action_input": "", "final_answer": ""}
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("Expected one JSON object")
        fields = {}
        for key, value in payload.items():
            name = key.strip().lower().replace(" ", "_")
            if name in result:
                if name in fields:
                    raise ValueError("Ambiguous ReAct JSON fields")
                fields[name] = value
        # Ordinary JSON answers without ReAct fields remain plain answers.
        if not fields:
            return result
        for name in ("thought", "action", "final_answer"):
            value = fields.get(name)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{name} must be a string")
                result[name] = value.strip()
        if result["action"] and result["final_answer"]:
            raise ValueError("Expected an action or a final answer, not both")
        if result["final_answer"]:
            return result
        if not result["action"]:
            raise ValueError("ReAct JSON needs an action or a final answer")
        arguments = fields.get("action_input", {})
        if isinstance(arguments, str):
            arguments = json.loads(
                arguments,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        if not isinstance(arguments, dict):
            raise ValueError("action_input must be a JSON object")
        result["action_input"] = json.dumps(
            arguments, ensure_ascii=False, allow_nan=False
        )
        return result
    except (ValueError, RecursionError):
        # Do not expose JSON decoder messages or salvage a partial action: the
        # response may contain tool arguments, secrets, or quoted action markers.
        return {
            "thought": "",
            "action": "",
            "action_input": "",
            "final_answer": "",
            "parse_error": (
                "Expected one JSON object with either a nonempty action and "
                "object action_input, or a nonempty final_answer."
            ),
        }


@AgentRegistry.register("native_react")
class NativeReActAgent(ToolUsingAgent):
    """ReAct agent: Thought -> Action -> Observation loop."""

    agent_id = "native_react"
    _default_temperature = 0.7
    _default_max_tokens = 1024
    _default_max_turns = 10

    def __init__(
        self,
        engine: InferenceEngine,
        model: str,
        *,
        tools: Optional[List[BaseTool]] = None,
        bus: Optional[EventBus] = None,
        max_turns: Optional[int] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        interactive: bool = False,
        confirm_callback=None,
        skill_few_shot_examples: Optional[List[str]] = None,
        capability_policy: Optional[Any] = None,
        agent_id: Optional[str] = None,
        rate_limiter: Optional[Any] = None,
        prompt_builder: Optional[Any] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            tools=tools,
            bus=bus,
            max_turns=max_turns,
            temperature=temperature,
            max_tokens=max_tokens,
            interactive=interactive,
            confirm_callback=confirm_callback,
            skill_few_shot_examples=skill_few_shot_examples,
            capability_policy=capability_policy,
            agent_id=agent_id,
            rate_limiter=rate_limiter,
            prompt_builder=prompt_builder,
        )

    def _parse_response(self, text: str) -> dict:
        """Parse ReAct structured output."""
        stripped = text.strip()
        fenced = re.fullmatch(
            r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```",
            stripped,
            re.DOTALL | re.IGNORECASE,
        )
        if fenced and fenced.group(1).lstrip().startswith(("{", "[")):
            return _parse_json_response(fenced.group(1))
        if (
            stripped.startswith("{")
            or re.match(r"\[\s*\{", stripped)
            or stripped.lower().startswith("```json")
            or re.match(r"```[ \t]*\r?\n\s*[\{\[]", stripped)
        ):
            return _parse_json_response(stripped)

        result = {"thought": "", "action": "", "action_input": "", "final_answer": ""}

        # Extract Thought
        thought_match = re.search(
            r"Thought:\s*(.+?)(?=\nAction:|\nFinal Answer:|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if thought_match:
            result["thought"] = thought_match.group(1).strip()

        # Check for Final Answer
        final_match = re.search(
            r"Final Answer:\s*(.+)", text, re.DOTALL | re.IGNORECASE
        )
        if final_match:
            result["final_answer"] = final_match.group(1).strip()
            return result

        # Extract Action and Action Input
        action_match = re.search(r"Action:\s*(.+)", text, re.IGNORECASE)
        if action_match:
            result["action"] = action_match.group(1).strip()

        input_match = re.search(
            r"Action Input:\s*(.+?)(?=\n\n|\nThought:|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if input_match:
            result["action_input"] = input_match.group(1).strip()

        return result

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        self._emit_turn_start(input)

        # Build system prompt with rich tool descriptions
        tool_desc = build_tool_descriptions(self._tools)
        # Plan 2B I3: render optimized few-shot skill examples as a section
        # before the tool descriptions. Empty string when not present.
        if self._skill_few_shot_examples:
            skill_examples_block = (
                "## Skill Examples\n\n"
                + "\n\n".join(self._skill_few_shot_examples)
                + "\n\n"
            )
        else:
            skill_examples_block = ""
        # Respect $OPENJARVIS_HOME override for the base template (M2+ work).
        prompt_template = (
            load_system_prompt_override("native_react") or REACT_SYSTEM_PROMPT
        )
        # External overrides may not include the {skill_examples} slot.
        try:
            system_prompt = prompt_template.format(
                tool_descriptions=tool_desc,
                skill_examples=skill_examples_block,
            )
        except KeyError:
            system_prompt = prompt_template.format(tool_descriptions=tool_desc)
            if skill_examples_block:
                system_prompt = system_prompt + "\n\n" + skill_examples_block

        system_prompt = self._apply_persona(system_prompt)
        messages = self._build_messages(input, context, system_prompt=system_prompt)

        # Inject few-shot exemplars before the user input
        for ex in load_few_shot_exemplars("native_react"):
            if ex.get("input") and ex.get("output"):
                messages.insert(-1, Message(role=Role.USER, content=ex["input"]))
                messages.insert(-1, Message(role=Role.ASSISTANT, content=ex["output"]))

        all_tool_results: list[ToolResult] = []
        turns = 0
        total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for _turn in range(self._max_turns):
            turns += 1

            if self._loop_guard:
                messages = self._loop_guard.compress_context(messages)

            result = self._generate(messages)
            usage = result.get("usage", {})
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)

            content = result.get("content", "")
            parsed = self._parse_response(content)

            if parsed.get("parse_error"):
                self._emit_turn_end(turns=turns)
                return AgentResult(
                    content="Could not parse the model's ReAct response. "
                    + parsed["parse_error"],
                    tool_results=all_tool_results,
                    turns=turns,
                    metadata={
                        **total_usage,
                        "response_parse_error": parsed["parse_error"],
                        "messages": [_message_to_dict(m) for m in messages],
                    },
                )

            # Anti-hallucination check for fake action claims without tool execution
            fake_pattern = (
                r"(ha sido enviado|he enviado|mensaje enviado|se envi[oó]|se ha enviado|captura tomada|"
                r"he tomado la captura|captura enviada|evento creado|evento agendado|tarea creada|"
                r"he creado la tarea|archivo modificado|correo enviado|chat\s+.*\babierto\b|"
                r"abierto\s+.*\bchat\b|abierto\s+y\s+enfocado|en primer plano|tienes\s+.*\bprimer plano\b|"
                r"proyecto\s+.*\babierto\b|carpeta\s+.*\babierta\b|aplicaci[oó]n\s+.*\babierta\b|"
                r"programa\s+.*\babierto\b|calculadora\s+.*\babierta\b|dolphin\s+.*\babierto\b|"
                r"chrome\s+.*\babierto\b|listo.*enviado|listo.*abierto|"
                r"agente.*ha respondido|antigravity.*ha respondido|el agente.*ha procesado)"
            )
            target_check = parsed.get("final_answer") or content
            if not all_tool_results and turns < (self._max_turns or 10) - 1:
                m_claim = re.search(fake_pattern, target_check, re.IGNORECASE)
                if m_claim:
                    logger.warning("Intercepted hallucinated action claim without tool call: %s", m_claim.group(0))
                    messages.append(Message(role=Role.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=Role.USER,
                            content=(
                                f"ERROR DE VERACIDAD: Afirmaste '{m_claim.group(0)}', pero NO ejecutaste ninguna herramienta en este turno. "
                                "Tienes terminantemente prohibido fingir que enviaste un mensaje, abriste un chat, enfocaste una app, abriste una aplicación, tomaste una captura o ejecutaste una acción externa sin haber invocado la herramienta correspondiente. "
                                "Si requieres abrir o lanzar una aplicación (Calculadora, Dolphin, Chrome, ZapZap, etc.), invoca gui_automation con action='open_app' (o 'focus_app') y app='...'. "
                                "Si requieres abrir un chat de WhatsApp/ZapZap, invoca gui_automation con action='whatsapp_open_chat' y contact='...'. "
                                "Si requieres enviar un mensaje, invoca gui_automation con action='send_desktop_message' con text='...' y contact='...'. "
                                "Si no tienes una herramienta para hacerlo directamente, responde con honestidad total explicando tus límites."
                            ),
                        )
                    )
                    continue

            # Anti-hallucination check for fake IDE / Antigravity response claims
            ide_fake_pattern = (
                r"(agente.*(?:ha respondido|devolvi[oó]|proces[oó])|antigravity.*(?:ha respondido|dijo|respondi[oó])|"
                r"aquí tienes la respuesta que (?:me dio|ha dado)|respuesta del agente|el agente del ide ya ha procesado|"
                r"el agente de ia dentro de antigravity ide ha respondido)"
            )
            m_ide = re.search(ide_fake_pattern, target_check, re.IGNORECASE)
            if m_ide and turns < (self._max_turns or 10) - 1:
                logger.warning("Intercepted hallucinated IDE response claim: %s", m_ide.group(0))
                messages.append(Message(role=Role.ASSISTANT, content=content))
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            f"ERROR DE VERACIDAD CRÍTICO: Afirmaste '{m_ide.group(0)}'. "
                            "TÚ NO TIENES LA RESPUESTA DE ANTIGRAVITY. La herramienta 'ide_chat' solo envía la pregunta por teclado virtual a la IDE. "
                            "Antigravity apenas está pensando y redactando su respuesta en pantalla. Tienes terminantemente prohibido inventar o simular lo que Antigravity respondió. "
                            "Debes responderle a Elías únicamente confirmando que enviaste la pregunta a Antigravity en la IDE y pidiéndole con honestidad que espere un momento a que Antigravity termine de redactar su respuesta en pantalla."
                        ),
                    )
                )
                continue

            # Final answer?
            if parsed["final_answer"]:
                self._emit_turn_end(turns=turns)
                msg_dicts = [_message_to_dict(m) for m in messages]
                clean_ans = re.sub(
                    r"^(?:Final Answer|Respuesta Final):\s*",
                    "",
                    parsed["final_answer"],
                    flags=re.IGNORECASE,
                ).strip()
                # Clean up call-center cliches
                clean_ans = re.sub(
                    r"\s*¿(?:Necesitas|Hay algo|Deseas)[^?]+(?:listos por ahora|apoyarte|colaboro|más)[^?]*\?",
                    "",
                    clean_ans,
                    flags=re.IGNORECASE,
                ).strip()
                return AgentResult(
                    content=clean_ans or parsed["final_answer"],
                    tool_results=all_tool_results,
                    turns=turns,
                    metadata={**total_usage, "messages": msg_dicts},
                )

            # No action? Treat content as final answer
            if not parsed["action"]:
                self._emit_turn_end(turns=turns)
                msg_dicts = [_message_to_dict(m) for m in messages]
                clean = parsed.get("thought") or content
                clean = re.sub(
                    r"^(?:Thought|Pensamiento):\s*",
                    "",
                    clean,
                    flags=re.IGNORECASE,
                ).strip()
                clean = re.sub(
                    r"\s*¿(?:Necesitas|Hay algo|Deseas)[^?]+(?:listos por ahora|apoyarte|colaboro|más)[^?]*\?",
                    "",
                    clean,
                    flags=re.IGNORECASE,
                ).strip()
                return AgentResult(
                    content=clean or content,
                    tool_results=all_tool_results,
                    turns=turns,
                    metadata={**total_usage, "messages": msg_dicts},
                )

            # Execute action
            messages.append(Message(role=Role.ASSISTANT, content=content))

            tool_call = ToolCall(
                id=f"react_{turns}",
                name=parsed["action"],
                arguments=parsed["action_input"] or "{}",
            )

            # Loop guard check before execution
            if self._loop_guard:
                verdict = self._loop_guard.check_call(
                    tool_call.name,
                    tool_call.arguments,
                )
                if verdict.blocked:
                    tool_result = ToolResult(
                        tool_name=tool_call.name,
                        content=f"Loop guard: {verdict.reason}",
                        success=False,
                    )
                    all_tool_results.append(tool_result)
                    observation = f"Observation: {tool_result.content}"
                    messages.append(Message(role=Role.USER, content=observation))
                    continue

            tool_result = self._executor.execute(tool_call)
            all_tool_results.append(tool_result)

            observation = f"Observation: {tool_result.content}"
            messages.append(Message(role=Role.USER, content=observation))

        # Max turns exceeded
        msg_dicts = [_message_to_dict(m) for m in messages]
        return self._max_turns_result(
            all_tool_results,
            turns,
            metadata={**total_usage, "messages": msg_dicts},
        )


__all__ = ["NativeReActAgent", "REACT_SYSTEM_PROMPT"]
