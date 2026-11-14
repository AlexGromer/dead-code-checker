package com.acme.shop;

import org.springframework.stereotype.Service;

@Service
public class StandardPriceService implements PriceService {
    @Override
    public long price(String sku) {
        return base(sku) + 1;
    }

    private long base(String sku) {
        return sku.length();
    }

    private long unusedMargin(long cents) {
        return cents * 2;
    }
}
