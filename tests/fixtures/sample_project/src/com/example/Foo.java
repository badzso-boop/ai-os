package com.example;

import com.example.other.Helper;

public class Foo extends Base implements Bar {
    private int x;

    /**
     * Returns doubled x via the shared Helper.
     */
    public int getX() {
        return Helper.compute(this.x);
    }
}
