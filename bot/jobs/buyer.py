import logging

from telegram import Bot

from agent.buyer_graph import buyer_graph
from config import settings
from db.client import get_setting, save_offer, set_task_status

logger = logging.getLogger(__name__)

HOME_KEY = "home_location"
CURRENT_KEY = "current_location"


async def run_buyer(
    task_id: int,
    task_text: str,
    search_query: str,
    location_type: str,
    bot: Bot,
    strategy: str = "any",
    deadline_days: int | None = None,
) -> None:
    await set_task_status(task_id, "processing")
    try:
        home_location = await get_setting(HOME_KEY) or settings.home_location
        current_location = await get_setting(CURRENT_KEY) or home_location

        state = await buyer_graph.ainvoke({
            "task_text": task_text,
            "search_query": search_query,
            "strategy": strategy,
            "deadline_days": deadline_days,
            "current_location": current_location,
            "home_location": home_location,
            "offers": [],
        })
        offers = state.get("offers", [])

        # Determine label for the location used
        if location_type == "local":
            loc_label = f"📍 {current_location}" if current_location else "📍 local"
        elif location_type == "online":
            loc_label = "🌐 online"
        else:
            loc_label = f"📍 {current_location}" if current_location else "🌐 anywhere"

        for offer in offers:
            await save_offer(
                task_id=task_id,
                title=offer.title,
                price=offer.price,
                store=offer.store,
                url=offer.url,
                snippet=offer.snippet,
                location_context=location_type,
                delivery_days_estimate=offer.delivery_days,
            )

        await set_task_status(task_id, "done")

        if offers:
            lines = [f"Found {len(offers)} offers {loc_label}:\n"]
            for o in offers[:5]:
                price_str = f" — {o.price}" if o.price else ""
                lines.append(f"• [{o.store}{price_str}]({o.url})")
            if len(offers) > 5:
                lines.append(f"_+{len(offers) - 5} more in dashboard_")
            await bot.send_message(
                chat_id=settings.telegram_user_id,
                text="\n".join(lines),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            await bot.send_message(
                chat_id=settings.telegram_user_id,
                text=f"No offers found for task #{task_id}. Try rephrasing.",
            )

    except Exception as e:
        logger.exception("Buyer agent failed for task #%d: %s", task_id, e)
        await set_task_status(task_id, "error")
        await bot.send_message(
            chat_id=settings.telegram_user_id,
            text=f"Buyer agent failed for task #{task_id}: {e}",
        )
