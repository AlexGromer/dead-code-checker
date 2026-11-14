package com.acme.shop;

import org.springframework.stereotype.Service;

@Service
public class DiscountPriceService implements PriceService {
    @Override
    public long price(String sku) {
        return 1;
    }
}
