from django.shortcuts import render
import stripe
import requests
from django.conf import settings
from django.shortcuts import redirect, get_object_or_404, render
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
from django.template.response import TemplateResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from orders.models import Order
from cart.views import CartMixin
from decimal import Decimal
import json
import hashlib
import base64
import logging

logger = logging.getLogger(__name__)

# stripe login
# stripe listen --forward-to localhost:8000/payment/stripe/webhook/


stripe.api_key = settings.STRIPE_SECRET_KEY
stripe_endpoint_secret = settings.STRIPE_WEBHOOK_SECRET


def create_stripe_checkout_session(order, request):
    cart = CartMixin().get_cart(request)
    line_items = []
    for item in cart.items.select_related('product', 'product_size'):
        line_items.append({
            'price_data': {
                'currency': 'eur',
                'product_data': {
                    'name': f'{item.product.name} - {item.product_size.size.name}',
                },
                'unit_amount': int(item.product.price * 100),
            },
            'quantity': item.quantity,
        })
    
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=line_items,
            mode='payment',
            success_url=request.build_absolute_uri('/payment/stripe/success/') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.build_absolute_uri('/payment/stripe/cancel/') + f'order_id={order.id}',
            metadata={
                'order_id': order.id
            }
        )
        order.stripe_payment_intent_id = checkout_session.payment_intent
        order.payment_provider = 'stripe'
        order.save()
        return checkout_session
    except Exception as e:
        raise


@csrf_exempt
@require_POST
def stripe_webhook(request):
    if not stripe_endpoint_secret:
        return HttpResponse(status=500)
    
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, stripe_endpoint_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)
    
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        # ПРОСТОЙ СПОСОБ: преобразуем весь session в словарь
        session_dict = session.to_dict()
        order_id = session_dict.get('metadata', {}).get('order_id')
        
        if order_id:
            try:
                order = Order.objects.get(id=order_id)
                order.status = 'processing'
                if session_dict.get('payment_intent'):
                    order.stripe_payment_intent_id = session_dict.get('payment_intent')
                order.save()
                print(f"✅ Webhook: Order {order.id} status updated to processing")
            except Order.DoesNotExist:
                print(f"❌ Webhook: Order {order_id} not found")
                return HttpResponse(status=400)
        else:
            print(f"⚠️ Webhook: No order_id in metadata: {session_dict.get('metadata')}")
            
    return HttpResponse(status=200)

def stripe_success(request):
    session_id = request.GET.get('session_id')
    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            
            # ИСПРАВЛЕНО: используем правильный способ получения metadata
            # Вариант 1: как атрибут
            if hasattr(session.metadata, 'order_id'):
                order_id = session.metadata.order_id
            # Вариант 2: через getattr
            else:
                order_id = getattr(session.metadata, 'order_id', None)
            
            # Альтернативный вариант - преобразовать в словарь
            # order_id = dict(session.metadata).get('order_id')
            
            if not order_id:
                # Попробуем получить через индексацию
                try:
                    order_id = session['metadata']['order_id']
                except:
                    logger.error(f"Cannot get order_id from metadata. Metadata: {session.metadata}")
                    return redirect('main:index')
            
            order = get_object_or_404(Order, id=order_id)

            cart = CartMixin().get_cart(request)
            cart.clear()

            context = {'order': order}
            if request.headers.get('HX-Request'):
                return TemplateResponse(request, 'payment/stripe_success_content.html', context)
            return render(request, 'payment/stripe_success.html', context)
        except Exception as e:
            logger.error(f"Error in stripe_success: {e}", exc_info=True)
            return redirect('main:index')
    return redirect('main:index')


def stripe_cancel(request):
    order_id = request.GET.get('order_id')
    if order_id:
        order = get_object_or_404(Order, id=order_id)
        order.status = 'cancelled'
        order.save()
        context = {'order': order}
        if request.headers.get('HX-Request'):
            return TemplateResponse(request, 'payment/stripe_cancel_content.html', context)
        return render(request, 'payment/stripe_cancel.html', context)
    return redirect('orders:checkout')