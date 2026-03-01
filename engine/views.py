from django.shortcuts import render, redirect
from database import get_db
from common.models.db import User
from common.models.tariff import ALL_TARIFFS, str_to_tariff
# Если нужно логировать платеж, импортируй и YkPayment

def index(request):
    # Просто передаем список тарифов из твоего Pydantic-файла в шаблон
    return render(request, 'index.html', {'tariffs': ALL_TARIFFS})

def second(request):
    # Просто передаем список тарифов из твоего Pydantic-файла в шаблон
    return render(request, 'second.html', {'tariffs': ALL_TARIFFS})

def buy(request):
    if request.method == "POST":
        email = request.POST.get("email").lower().strip()
        tariff_id = request.POST.get("tariff_id")
        
        # Тут будет логика создания платежа ЮKassa, которую мы обсуждали.
        # Пока просто выведем в консоль для теста:
        print(f"Заказ от {email} на тариф {tariff_id}")
        
        return redirect('index') # Временно редиректим обратно