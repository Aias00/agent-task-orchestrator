"""Interaction command handling; runtime context is supplied by the public facade."""
import json

def handle(args, core):
    if args.command in ('hub', 'ask', 'inbox', 'read-question', 'read', 'answer', 'respond', 'accept-answer', 'continue-task'):
        from ato_adapters import agents
        from ato_workflow import interaction
        proof, reference, bound_workflow = (None, None, None)
        if args.actor_file:
            from ato_adapters.local_session_adapter import load_actor_file
            proof, reference, bound_workflow = load_actor_file(args.actor_file)
        if args.command == 'hub':
            if args.thread and args.session_ref:
                raise ValueError('select either a legacy thread or a session reference')
            if (args.thread or args.session_ref) and (not args.source):
                raise ValueError('hub binding requires a user instruction source')
            result = interaction.configure_session(args.session_ref, args.source, actor_proof=proof) if args.session_ref else interaction.configure(args.thread, args.source) if args.thread else interaction.config()
        elif args.command == 'ask':
            result = interaction.ask(args.directory, args.question, args.recommendation, args.context, args.request, actor_proof=proof)
        elif args.command == 'inbox':
            result = interaction.inbox(actor_proof=proof, reference=reference, workflow=bound_workflow)
        elif args.command in ('read-question', 'read'):
            result = interaction.read_question(args.identifier, actor_proof=proof, presented_source=args.presented_source)
        elif args.command in ('answer', 'respond'):
            result = interaction.answer(args.identifier, args.answer, args.source, actor_proof=proof)
        elif args.command == 'accept-answer':
            result = interaction.accept(args.identifier, budget_stop_evidence=args.budget_stop_evidence, actor_proof=proof)
        else:
            from ato_workflow.workflow import Workflow
            Workflow(args.directory).execute(continuation=(args.accepted_answer, proof))
            result = core.status(args.directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    raise ValueError('command outside interaction_cmd domain')
