"""
Application services containing business logic
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Protocol
from uuid import UUID, uuid4

from domain import User, Auction, Bid, AuctionStatus
from repositories import UserRepository, AuctionRepository


class NotificationService(Protocol):
    """Notification service interface"""
    async def notify_bid_placed(self, auction: Auction, new_bid: Bid) -> None: ...
    async def notify_auction_ended(self, auction: Auction) -> None: ...
    async def notify_bid_overtaken(self, auction: Auction, overtaken_user_id: int, new_bid: Bid) -> None: ...
    async def notify_auction_started(self, auction: Auction) -> None: ...
    async def broadcast_current_auction(self, auction: Auction, user_id: int) -> None: ...


class AuctionService:
    """Main auction business logic service"""
    
    def __init__(self, 
                 user_repo: UserRepository, 
                 auction_repo: AuctionRepository,
                 notification_service: NotificationService):
        self.user_repo = user_repo
        self.auction_repo = auction_repo
        self.notification_service = notification_service

    async def register_user(self, user_id: int, username: str, telegram_handle: Optional[str] = None, 
                           first_name: Optional[str] = None, last_name: Optional[str] = None) -> bool:
        """Register a new user"""
        existing_user = await self.user_repo.get_user_by_username(username)
        if existing_user:
            return False
        
        admin_ids = os.getenv('ADMIN_IDS', '').split(',')
        is_admin = str(user_id) in admin_ids
        
        user = User(
            user_id=user_id,
            username=username,
            telegram_handle=telegram_handle,
            first_name=first_name,
            last_name=last_name,
            is_admin=is_admin
        )
        await self.user_repo.save_user(user)
        return True

    async def create_auction(self, creator_id: int, title: str, start_price: float, 
                           duration_hours: int, description: Optional[str] = None,
                           photo_url: Optional[str] = None, media_type: str = 'photo',
                           custom_message: Optional[str] = None) -> UUID:
        """Create a new auction - active if no active auctions, scheduled otherwise"""
        auction_id = uuid4()
        
        # Check if there are active auctions
        active_auctions = await self.auction_repo.get_active_auctions()
        
        if active_auctions:
            # Schedule auction for after current auctions end
            latest_end_time = max((a.end_time for a in active_auctions if a.end_time), default=datetime.now())
            start_time = latest_end_time + timedelta(minutes=1)  # Start 1 minute after previous ends
            end_time = start_time + timedelta(hours=duration_hours) if duration_hours > 0 else None
            status = AuctionStatus.SCHEDULED
        else:
            # Start immediately
            start_time = datetime.now()
            end_time = start_time + timedelta(hours=duration_hours) if duration_hours > 0 else None
            status = AuctionStatus.ACTIVE
        
        auction = Auction(
            auction_id=auction_id,
            title=title,
            description=description,
            photo_url=photo_url,
            media_type=media_type,
            custom_message=custom_message,
            start_price=start_price,
            current_price=start_price,
            status=status,
            creator_id=creator_id,
            participants=[],
            bids=[],
            created_at=datetime.now(),
            start_time=start_time,
            end_time=end_time
        )
        
        await self.auction_repo.save_auction(auction)
        return auction_id

    async def activate_scheduled_auction(self, auction_id: UUID) -> bool:
        """Activate a scheduled auction"""
        auction = await self.auction_repo.get_auction(auction_id)
        if not auction or auction.status != AuctionStatus.SCHEDULED:
            return False
        
        auction.status = AuctionStatus.ACTIVE
        await self.auction_repo.update_auction(auction)
        
        # Notify users about new auction
        await self.notification_service.notify_auction_started(auction)
        return True

    async def get_current_auction(self) -> Optional[Auction]:
        """Get the current active auction for users"""
        active_auctions = await self.auction_repo.get_active_auctions()
        return active_auctions[0] if active_auctions else None

    async def get_next_scheduled_auction(self) -> Optional[Auction]:
        """Get the next scheduled auction"""
        scheduled_auctions = await self.auction_repo.get_scheduled_auctions()
        return scheduled_auctions[0] if scheduled_auctions else None

    async def join_auction(self, auction_id: UUID, user_id: int) -> bool:
        """Join an auction as participant"""
        auction = await self.auction_repo.get_auction(auction_id)
        if not auction or not auction.is_active:
            return False
        
        user = await self.user_repo.get_user(user_id)
        if not user or user.is_blocked:
            return False
        
        if user_id not in auction.participants:
            auction.participants.append(user_id)
            await self.auction_repo.update_auction(auction)
        
        return True

    async def place_bid(self, auction_id: UUID, user_id: int, amount: float) -> bool:
        """Place a bid on an auction"""
        auction = await self.auction_repo.get_auction(auction_id)
        if not auction or not auction.is_active:
            return False
        
        user = await self.user_repo.get_user(user_id)
        if not user or user.is_blocked:
            return False
        
        if user_id not in auction.participants:
            return False
        
        if amount <= auction.current_price:
            return False
        
        # Remember previous leader
        previous_leader = auction.current_leader
        
        bid = Bid(
            bid_id=uuid4(),
            auction_id=auction_id,
            user_id=user_id,
            amount=amount,
            created_at=datetime.now(),
            username=user.username
        )
        
        auction.bids.append(bid)
        auction.current_price = amount
        await self.auction_repo.update_auction(auction)
        
        # Send notifications
        await self.notification_service.notify_bid_placed(auction, bid)
        
        if previous_leader and previous_leader.user_id != user_id:
            await self.notification_service.notify_bid_overtaken(auction, previous_leader.user_id, bid)
        
        return True

    async def end_auction(self, auction_id: UUID, admin_id: int) -> bool:
        """End an auction manually"""
        auction = await self.auction_repo.get_auction(auction_id)
        if not auction or auction.status != AuctionStatus.ACTIVE:
            return False
        
        admin = await self.user_repo.get_user(admin_id)
        if not admin or not admin.is_admin:
            return False
        
        auction.status = AuctionStatus.COMPLETED
        leader = auction.current_leader
        if leader:
            auction.winner_id = leader.user_id
        
        await self.auction_repo.update_auction(auction)
        await self.notification_service.notify_auction_ended(auction)
        return True

    async def get_user_status(self, user_id: int) -> Dict:
        """Get user status and participation info"""
        user = await self.user_repo.get_user(user_id)
        if not user:
            return {"registered": False}
        
        auctions = await self.auction_repo.get_active_auctions()
        participating_in = []
        
        for auction in auctions:
            if user_id in auction.participants:
                user_bid = None
                for bid in auction.bids:
                    if bid.user_id == user_id:
                        if not user_bid or bid.amount > user_bid.amount:
                            user_bid = bid
                
                participating_in.append({
                    "auction": auction,
                    "user_bid": user_bid,
                    "is_leader": auction.current_leader and auction.current_leader.user_id == user_id
                })
        
        return {
            "registered": True,
            "user": user,
            "participating_in": participating_in
        }


class TelegramNotificationService:
    """Telegram-specific notification implementation"""
    
    def __init__(self, application):
        self.application = application
        self.user_repo = None  # Will be injected

    async def notify_bid_placed(self, auction: Auction, new_bid: Bid) -> None:
        """Notify participants about new bid"""
        message = f"🔥 Новая ставка в аукционе *{auction.title}*\n\n"
        message += f"👤 {new_bid.username} — *{new_bid.amount:,.0f}₽*"
        
        # Notify all participants except bid author
        for participant_id in auction.participants:
            if participant_id != new_bid.user_id:
                try:
                    await self.application.bot.send_message(
                        chat_id=participant_id,
                        text=message,
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logging.error(f"Failed to notify user {participant_id}: {e}")

        # Notify bid author
        try:
            await self.application.bot.send_message(
                chat_id=new_bid.user_id,
                text=f"✅ Ваша ставка *{new_bid.amount:,.0f}₽* теперь лидирует в аукционе *{auction.title}*!",
                parse_mode='Markdown'
            )
        except Exception as e:
            logging.error(f"Failed to notify bid author {new_bid.user_id}: {e}")

    async def notify_bid_overtaken(self, auction: Auction, overtaken_user_id: int, new_bid: Bid) -> None:
        """Notify user their bid was overtaken"""
        try:
            await self.application.bot.send_message(
                chat_id=overtaken_user_id,
                text=f"😔 Вашу ставку перебили в аукционе *{auction.title}*\n\n"
                     f"Новый лидер: {new_bid.username} — *{new_bid.amount:,.0f}₽*",
                parse_mode='Markdown'
            )
        except Exception as e:
            logging.error(f"Failed to notify overtaken user {overtaken_user_id}: {e}")

    async def notify_auction_ended(self, auction: Auction) -> None:
        """Notify all participants auction ended"""
        winner = auction.current_leader
        message = f"🏁 Аукцион *{auction.title}* завершён!\n\n"
        
        if winner:
            # Get winner display name
            if self.user_repo:
                winner_user = await self.user_repo.get_user(winner.user_id)
                winner_name = winner_user.display_name if winner_user else winner.username
            else:
                winner_name = winner.username
            
            message += f"🏆 Победитель: {winner_name}\n"
            message += f"💰 Итоговая ставка: *{winner.amount:,.0f}₽*\n"
        else:
            message += "❌ Ставок не было\n"
        
        message += f"👥 Участников: {len(auction.participants)}\n"
        message += f"📊 Всего ставок: {len(auction.bids)}"
        
        # Notify all participants
        for participant_id in auction.participants:
            try:
                await self.application.bot.send_message(
                    chat_id=participant_id,
                    text=message,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logging.error(f"Failed to notify participant {participant_id}: {e}")

    async def notify_auction_started(self, auction: Auction) -> None:
        """Notify all users about new auction"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        welcome_msg = auction.custom_message or "🎉 *Новый аукцион начался!*"
        auction_message = await self._format_auction_message(auction)
        keyboard = self._get_auction_keyboard(auction.auction_id)
        
        # Get all users
        if self.user_repo:
            all_users = await self.user_repo.get_all_users()
            
            for user in all_users:
                if user.is_blocked or user.is_admin:
                    continue
                
                try:
                    await self.application.bot.send_message(
                        chat_id=user.user_id,
                        text=welcome_msg,
                        parse_mode='Markdown'
                    )
                    
                    if auction.photo_url:
                        if auction.media_type == 'photo':
                            await self.application.bot.send_photo(
                                chat_id=user.user_id,
                                photo=auction.photo_url,
                                caption=auction_message,
                                parse_mode='Markdown',
                                reply_markup=keyboard
                            )
                        elif auction.media_type == 'video':
                            await self.application.bot.send_video(
                                chat_id=user.user_id,
                                video=auction.photo_url,
                                caption=auction_message,
                                parse_mode='Markdown',
                                reply_markup=keyboard
                            )
                        elif auction.media_type == 'animation':
                            await self.application.bot.send_animation(
                                chat_id=user.user_id,
                                animation=auction.photo_url,
                                caption=auction_message,
                                parse_mode='Markdown',
                                reply_markup=keyboard
                            )
                    else:
                        await self.application.bot.send_message(
                            chat_id=user.user_id,
                            text=auction_message,
                            parse_mode='Markdown',
                            reply_markup=keyboard
                        )
                except Exception as e:
                    logging.error(f"Failed to notify user {user.user_id} about new auction: {e}")

    async def broadcast_current_auction(self, auction: Auction, user_id: int) -> None:
        """Send current auction to specific user"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        message = await self._format_auction_message(auction)
        keyboard = self._get_auction_keyboard(auction.auction_id, user_id in auction.participants)
        
        try:
            if auction.photo_url:
                if auction.media_type == 'photo':
                    await self.application.bot.send_photo(
                        chat_id=user_id,
                        photo=auction.photo_url,
                        caption=message,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                elif auction.media_type == 'video':
                    await self.application.bot.send_video(
                        chat_id=user_id,
                        video=auction.photo_url,
                        caption=message,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                elif auction.media_type == 'animation':
                    await self.application.bot.send_animation(
                        chat_id=user_id,
                        animation=auction.photo_url,
                        caption=message,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
            else:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
        except Exception as e:
            logging.error(f"Failed to send auction to user {user_id}: {e}")

    async def _format_auction_message(self, auction: Auction) -> str:
        """Format auction information message"""
        message = f"🎯 *{auction.title}*\n\n"
        
        if auction.description:
            message += f"📄 {auction.description}\n\n"
        
        message += f"💰 Текущая цена: *{auction.current_price:,.0f}₽*\n"
        
        leader = auction.current_leader
        if leader:
            # Get user display name if possible
            if self.user_repo:
                leader_user = await self.user_repo.get_user(leader.user_id)
                leader_name = leader_user.display_name if leader_user else leader.username
            else:
                leader_name = leader.username
            message += f"👤 Лидер: {leader_name}\n"
        
        message += f"👥 Участников: {len(auction.participants)}\n"
        message += f"📊 Ставок: {len(auction.bids)}\n"
        
        if auction.is_scheduled:
            if auction.time_until_start:
                message += f"⏰ Начнется через: {auction.time_until_start}\n"
            else:
                message += "⏰ Готов к запуску\n"
        elif auction.time_remaining:
            message += f"⏰ Осталось: {auction.time_remaining}\n"
        else:
            message += "⏰ Бессрочный\n"
        
        return message

    def _get_auction_keyboard(self, auction_id: UUID, is_participant: bool = False) -> 'InlineKeyboardMarkup':
        """Generate auction inline keyboard"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        keyboard = []
        
        if not is_participant:
            keyboard.append([InlineKeyboardButton("✅ Участвовать", callback_data=f"join_{auction_id}")])
        else:
            keyboard.append([InlineKeyboardButton("💸 Перебить ставку", callback_data=f"bid_{auction_id}")])
        
        keyboard.append([InlineKeyboardButton("ℹ️ Обновить статус", callback_data=f"status_{auction_id}")])
        keyboard.append([InlineKeyboardButton("📱 Главное меню", callback_data="main_menu")])
        
        return InlineKeyboardMarkup(keyboard)


class AuctionScheduler:
    """Scheduler for automatic auction ending and activation"""
    
    def __init__(self, auction_service: AuctionService, auction_repo: AuctionRepository):
        self.auction_service = auction_service
        self.auction_repo = auction_repo
        self.running = False

    async def start(self):
        """Start the scheduler loop"""
        self.running = True
        while self.running:
            try:
                await self._check_expired_auctions()
                await self._check_scheduled_auctions()
                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                logging.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)

    async def stop(self):
        """Stop the scheduler"""
        self.running = False

    async def _check_expired_auctions(self):
        """Check and end expired auctions"""
        auctions = await self.auction_repo.get_active_auctions()
        now = datetime.now()
        
        for auction in auctions:
            if auction.end_time and now >= auction.end_time:
                auction.status = AuctionStatus.COMPLETED
                leader = auction.current_leader
                if leader:
                    auction.winner_id = leader.user_id
                
                await self.auction_repo.update_auction(auction)
                await self.auction_service.notification_service.notify_auction_ended(auction)
                logging.info(f"Auto-ended auction: {auction.title}")

    async def _check_scheduled_auctions(self):
        """Check if we need to activate scheduled auctions"""
        active_auctions = await self.auction_repo.get_active_auctions()
        if not active_auctions:  # No active auctions
            scheduled_auctions = await self.auction_repo.get_scheduled_auctions()
            if scheduled_auctions:
                # Activate the first scheduled auction
                next_auction = scheduled_auctions[0]
                now = datetime.now()
                if now >= next_auction.start_time:
                    await self.auction_service.activate_scheduled_auction(next_auction.auction_id)
                    logging.info(f"Auto-activated scheduled auction: {next_auction.title}")